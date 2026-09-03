"""Signed start links and single-use OAuth state."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db.models import User
from strikt.integrations import oauth
from tests.test_integrations_fakes import NOW, make_settings


def test_sign_and_verify_round_trip() -> None:
    settings = make_settings()
    secret = oauth.link_secret(settings)
    token = oauth.sign_user(42, secret=secret, now=NOW)
    assert "." in token
    assert oauth.verify_user(token, secret=secret, now=NOW + timedelta(hours=1)) == 42


def test_verify_rejects_tampering_and_other_secret() -> None:
    settings = make_settings()
    secret = oauth.link_secret(settings)
    token = oauth.sign_user(42, secret=secret, now=NOW)
    payload, mac = token.split(".")
    forged = oauth.sign_user(43, secret=secret, now=NOW).split(".")[0] + "." + mac
    with pytest.raises(oauth.LinkError):
        oauth.verify_user(forged, secret=secret, now=NOW)
    with pytest.raises(oauth.LinkError):
        oauth.verify_user(token, secret=oauth.link_secret(make_settings()), now=NOW)
    with pytest.raises(oauth.LinkError):
        oauth.verify_user("garbage", secret=secret, now=NOW)
    with pytest.raises(oauth.LinkError):
        oauth.verify_user(payload, secret=secret, now=NOW)


def test_verify_rejects_expired_links() -> None:
    secret = oauth.link_secret(make_settings())
    token = oauth.sign_user(1, secret=secret, now=NOW)
    with pytest.raises(oauth.LinkError):
        oauth.verify_user(token, secret=secret, now=NOW + timedelta(days=2))
    assert oauth.verify_user(token, secret=secret, now=NOW + timedelta(hours=23)) == 1


def test_link_secret_prefers_dedicated_setting_and_requires_a_key() -> None:
    plain = make_settings()
    derived = oauth.link_secret(plain)
    assert len(derived) == 32
    empty = make_settings(token_encryption_key="")
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        oauth.link_secret(empty)


def test_urls_and_slugs() -> None:
    settings = make_settings(public_base_url="https://coach.example.com/")
    assert oauth.callback_url(settings, "whoop") == "https://coach.example.com/oauth/whoop/callback"
    assert (
        oauth.webhook_url(settings, "apple_health", "tok")
        == "https://coach.example.com/webhooks/apple-health/tok"
    )
    assert oauth.webhook_url(settings, "withings") == "https://coach.example.com/webhooks/withings"
    assert oauth.provider_from_slug("apple-health") == "apple_health"
    assert oauth.provider_from_slug("apple_health") == "apple_health"
    assert oauth.provider_from_slug("WHOOP") == "whoop"
    assert oauth.provider_from_slug("garmin") is None
    url = oauth.start_url(settings, "whoop", 7, now=NOW)
    assert url.startswith("https://coach.example.com/oauth/whoop/start?u=")
    token = url.split("u=")[1]
    assert oauth.verify_user(token, secret=oauth.link_secret(settings), now=NOW) == 7


async def test_state_is_single_use_and_provider_bound(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    state = await oauth.issue_state(session, user.id, "whoop", now=clock.now())
    assert len(state) >= 8
    assert await oauth.consume_state(session, state, "withings", now=clock.now()) is None
    # the mismatched attempt already burned it (single use, whatever the outcome)
    assert await oauth.consume_state(session, state, "whoop", now=clock.now()) is None

    fresh = await oauth.issue_state(session, user.id, "whoop", now=clock.now())
    assert await oauth.consume_state(session, fresh, "whoop", now=clock.now()) == user.id
    assert await oauth.consume_state(session, fresh, "whoop", now=clock.now()) is None

    stale = await oauth.issue_state(session, user.id, "whoop", now=clock.now())
    later = clock.now() + timedelta(minutes=11)
    assert await oauth.consume_state(session, stale, "whoop", now=later) is None
    assert await oauth.consume_state(session, "", "whoop", now=clock.now()) is None
    assert await oauth.consume_state(session, "x" * 65, "whoop", now=clock.now()) is None
