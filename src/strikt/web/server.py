"""aiohttp application (PLAN §9): health, OAuth start/callback, provider webhooks, Telegram.

Routes:

- ``GET  /health`` → ``{"status": "ok", "providers": [...]}``
- ``GET  /oauth/{provider}/start?u=<signed>`` → verifies the signed user link, asks the provider
  for its ``ConnectInfo`` and redirects to the authorize URL (or shows webhook instructions).
- ``GET  /oauth/{provider}/callback`` → ``Integration.handle_callback``; shows a one-line result.
- ``POST|HEAD /webhooks/{provider}`` and ``/webhooks/{provider}/{token}`` → builds a
  ``WebhookRequest``, calls the integration, publishes the returned events on the bus and
  answers inside the handler.
- ``POST /telegram`` when a Telegram webhook handler is given.

Everything runs in the bot's process; ``run_server`` starts an ``AppRunner`` and returns it so
the caller can ``await runner.cleanup()`` on shutdown.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

from strikt.core.clock import Clock, SystemClock
from strikt.db import repo
from strikt.integrations.base import Integration, ProviderName, WebhookRequest
from strikt.integrations.oauth import LinkError, link_secret, provider_from_slug, verify_user

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.db.models import User
    from strikt.events import EventBus

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], Any]
TelegramHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Notify = Callable[["User", str], Awaitable[None]]

SETTINGS_KEY: web.AppKey[Any] = web.AppKey("strikt_settings")
SESSIONS_KEY: web.AppKey[Any] = web.AppKey("strikt_sessions")
BUS_KEY: web.AppKey[Any] = web.AppKey("strikt_bus")
INTEGRATIONS_KEY: web.AppKey[Any] = web.AppKey("strikt_integrations")
CLOCK_KEY: web.AppKey[Any] = web.AppKey("strikt_clock")
NOTIFY_KEY: web.AppKey[Any] = web.AppKey("strikt_notify")

MAX_BODY = 5 * 1024 * 1024  # Health Auto Export batches can be large


# ---------------------------------------------------------------------------------- helpers


def _page(title: str, body: str, *, status: int = 200) -> web.Response:
    """A tiny self-contained HTML page (no assets, readable on a phone)."""
    text = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:17px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:32px 20px;"
        "max-width:560px;color:#111;background:#fafafa}pre{white-space:pre-wrap;word-break:break-word;"
        "font:inherit}h1{font-size:20px;margin:0 0 12px}</style></head>"
        f"<body><h1>{html.escape(title)}</h1><pre>{html.escape(body)}</pre></body></html>"
    )
    return web.Response(text=text, content_type="text/html", status=status)


def _provider(request: web.Request) -> tuple[ProviderName, Integration]:
    slug = request.match_info.get("provider", "")
    name = provider_from_slug(slug)
    integrations: Mapping[ProviderName, Integration] = request.app[INTEGRATIONS_KEY]
    if name is None or name not in integrations:
        raise web.HTTPNotFound(text="unknown provider")
    return name, integrations[name]


# --------------------------------------------------------------------------------- handlers


async def health(request: web.Request) -> web.Response:
    integrations: Mapping[str, Any] = request.app[INTEGRATIONS_KEY]
    return web.json_response({"status": "ok", "providers": sorted(integrations)})


async def oauth_start(request: web.Request) -> web.StreamResponse:
    name, integration = _provider(request)
    settings: Settings = request.app[SETTINGS_KEY]
    clock: Clock = request.app[CLOCK_KEY]
    token = request.query.get("u", "")
    try:
        user_id = verify_user(token, secret=link_secret(settings), now=clock.now())
    except (LinkError, ValueError) as exc:
        log.info("oauth_start_rejected", provider=name, reason=str(exc))
        return _page(
            "Strikt", "This link is invalid or expired. Ask for a new one in Telegram.", status=400
        )
    sessions: SessionFactory = request.app[SESSIONS_KEY]
    async with sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            return _page("Strikt", "Unknown user.", status=404)
        try:
            info = await integration.connect(session, user)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("oauth_start_failed", provider=name, user_id=user_id)
            return _page(
                "Strikt", "Could not start the connection. Try again in a minute.", status=500
            )
    if info.kind == "oauth" and info.url:
        raise web.HTTPFound(info.url)
    body = info.instructions or (info.url or "")
    return _page("Strikt", body)


async def oauth_callback(request: web.Request) -> web.Response:
    name, integration = _provider(request)
    sessions: SessionFactory = request.app[SESSIONS_KEY]
    notify: Notify | None = request.app[NOTIFY_KEY]
    query = dict(request.query.items())
    async with sessions() as session:
        try:
            user, message = await integration.handle_callback(session, query)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("oauth_callback_failed", provider=name)
            return _page(
                "Strikt",
                "The connection failed on our side. Ask for a new link in Telegram.",
                status=500,
            )
    if user is not None and notify is not None:
        try:
            await notify(user, message)
        except Exception as exc:
            log.warning("oauth_notify_failed", provider=name, user_id=user.id, error=repr(exc))
    log.info("oauth_callback", provider=name, user_id=user.id if user else None)
    return _page("Strikt", message, status=200 if user is not None else 400)


async def webhook(request: web.Request) -> web.Response:
    name, integration = _provider(request)
    sessions: SessionFactory = request.app[SESSIONS_KEY]
    bus: EventBus = request.app[BUS_KEY]
    body = b"" if request.method == "HEAD" else await request.read()
    inbound = WebhookRequest(
        provider=name,
        method=request.method,
        path=request.path,
        headers=dict(request.headers.items()),
        query=dict(request.query.items()),
        body=body,
        path_token=request.match_info.get("token"),
    )
    async with sessions() as session:
        try:
            response, events = await integration.handle_webhook(session, inbound)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("webhook_failed", provider=name)
            return web.Response(status=500, text="internal error")
    for event in events:
        try:
            await bus.publish(event)
        except Exception as exc:  # the bus already isolates handlers; this is belt and braces
            log.error("webhook_publish_failed", provider=name, error=repr(exc))
    log.info(
        "webhook", provider=name, method=request.method, status=response.status, events=len(events)
    )
    if request.method == "HEAD":
        return web.Response(status=response.status)
    return web.Response(
        status=response.status, text=response.body, content_type=response.content_type
    )


# ------------------------------------------------------------------------------------- app


def make_app(
    settings: Settings,
    session_factory: SessionFactory,
    bus: EventBus,
    integrations: Mapping[ProviderName, Integration],
    telegram_webhook_handler: TelegramHandler | None = None,
    *,
    clock: Clock | None = None,
    notify: Notify | None = None,
) -> web.Application:
    """Build the application. ``notify(user, message)`` (optional) tells the user in Telegram
    when an OAuth callback finished, so they do not have to read the browser page."""
    app = web.Application(client_max_size=MAX_BODY)
    app[SETTINGS_KEY] = settings
    app[SESSIONS_KEY] = session_factory
    app[BUS_KEY] = bus
    app[INTEGRATIONS_KEY] = dict(integrations)
    app[CLOCK_KEY] = clock or SystemClock()
    app[NOTIFY_KEY] = notify
    app.router.add_get("/health", health)
    app.router.add_get("/oauth/{provider}/start", oauth_start)
    app.router.add_get("/oauth/{provider}/callback", oauth_callback)
    for path in ("/webhooks/{provider}", "/webhooks/{provider}/{token}"):
        app.router.add_post(path, webhook)
        app.router.add_head(path, webhook)
    if telegram_webhook_handler is not None:
        app.router.add_post("/telegram", telegram_webhook_handler)
    return app


async def run_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start listening; returns the runner (``await runner.cleanup()`` to stop)."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("web_server_started", host=host, port=port)
    return runner
