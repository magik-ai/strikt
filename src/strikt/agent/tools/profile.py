"""Profile tools: update_profile, update_protocol, set_coaching_intensity, finish_onboarding,
connect_integration, import_history (PLAN §6.4, §10; brief §4, §7.3).

``update_profile`` is the only writer of profile fields (typed whitelist from the schema) and
marks onboarding steps through ``onboarding.checklist``; ``finish_onboarding`` refuses with the
list of what is missing until the brief's minimum set exists. Nothing here hard-codes a user's
numbers: everything arrives through these calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from strikt.agent.tools.common import build_state, fail, macros_dict, ok, rnd, state_numbers, to_utc
from strikt.core.clock import to_local
from strikt.db import repo
from strikt.db.models import (
    CoachingIntensity,
    ExplanationLevel,
    MeasurementType,
    PrimaryKpi,
    UserStatus,
)
from strikt.nutrition.math import kcal_from_macros, mismatch_ratio
from strikt.onboarding import checklist, importer
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)

USER_FIELDS: frozenset[str] = frozenset({"language", "timezone"})
ENUM_FIELDS: dict[str, type[Any]] = {
    "primary_kpi": PrimaryKpi,
    "coaching_intensity": CoachingIntensity,
    "explanation_level": ExplanationLevel,
}
SEND_FIRST: dict[str, str] = {
    "en": "Send your next meal as a photo or text. I log it and show the budget.",
    "ru": "Пришли следующий приём еды фото или текстом. Я запишу и покажу бюджет.",
}
PROTOCOL_KCAL_TOLERANCE = 0.10


def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


async def _facts(ctx: ToolContext) -> checklist.Facts:
    weight = await repo.latest_by_type(ctx.session, ctx.user_id, MeasurementType.weight)
    protocol = ctx.protocol or await repo.get_active_protocol(ctx.session, ctx.user_id)
    return checklist.facts_for(ctx.user, protocol, has_weight=weight is not None)


def _onboarding_block(ctx: ToolContext, facts: checklist.Facts) -> dict[str, Any]:
    profile = ctx.profile
    done = profile is not None and profile.onboarding_done_at is not None
    pending = checklist.next_step(profile, facts)
    return {
        "done": done,
        "step_marked": profile.onboarding_step if profile is not None else 0,
        "next_step": None if pending is None else f"{pending.id} {pending.key}",
        "missing_minimum": checklist.missing_minimum(profile, facts),
    }


async def update_profile(ctx: ToolContext, args: schemas.UpdateProfileInput) -> ToolResult:
    changes: dict[str, Any] = args.fields.model_dump(exclude_none=True, mode="python")
    if not changes:
        return fail("update_profile: nothing to save")
    now = ctx.clock.now()
    saved: list[str] = []

    language = changes.pop("language", None)
    timezone = changes.pop("timezone", None)
    if timezone is not None and not _valid_timezone(str(timezone)):
        return fail(f"update_profile: '{timezone}' is not an IANA timezone (e.g. Asia/Dubai)")
    if language is not None or timezone is not None:
        await repo.set_user_locale(ctx.session, ctx.user_id, language=language, timezone=timezone)
        if language is not None:
            ctx.user.language = str(language)
            saved.append("language")
        if timezone is not None:
            ctx.user.timezone = str(timezone)
            saved.append("timezone")

    if "training_plan" in changes and isinstance(changes["training_plan"], dict):
        changes["training_plan"] = {
            k: v for k, v in changes["training_plan"].items() if v is not None
        }
    for key, enum_cls in ENUM_FIELDS.items():
        if key in changes:
            changes[key] = enum_cls(changes[key])
    if "onboarding_step" in changes:
        current = ctx.profile.onboarding_step if ctx.profile is not None else 0
        step = int(changes["onboarding_step"])
        changes["onboarding_step"] = max(0, min(checklist.TOTAL_STEPS, max(current, step)))
    for key in ("height_cm", "waist_cadence_days", "weight_cadence_days"):
        if key in changes and float(changes[key]) <= 0:
            return fail(f"update_profile: {key} must be positive")

    unknown = set(changes) - repo.PROFILE_FIELDS
    if unknown:
        return fail(f"update_profile: unknown fields {sorted(unknown)}")
    if changes:
        ctx.profile = await repo.upsert_profile(ctx.session, ctx.user_id, changes, now=now)
        saved.extend(sorted(k for k in changes if k != "onboarding_step"))
    facts = await _facts(ctx)
    result: dict[str, Any] = {"saved": saved, "onboarding": _onboarding_block(ctx, facts)}
    if ctx.profile is not None and ctx.profile.onboarding_done_at is None:
        result["checklist"] = checklist.render_state(ctx.profile, ctx.lang, facts)
    log.info("profile_updated", user_id=ctx.user_id, fields=saved)
    return ok(result)


async def update_protocol(ctx: ToolContext, args: schemas.UpdateProtocolInput) -> ToolResult:
    if min(args.kcal, args.protein_g, args.fat_g, args.carbs_g) <= 0 or args.fiber_g < 0:
        return fail("update_protocol: kcal, protein, fat and carbs must be positive")
    computed = kcal_from_macros(args.protein_g, args.carbs_g, args.fat_g)
    ratio = mismatch_ratio(args.kcal, computed)
    protocol = await repo.set_active_protocol(
        ctx.session,
        ctx.user_id,
        kcal=args.kcal,
        protein_g=args.protein_g,
        fat_g=args.fat_g,
        carbs_g=args.carbs_g,
        fiber_g=args.fiber_g,
        rationale=" ".join(args.rationale.split()) or None,
        now=ctx.clock.now(),
    )
    ctx.protocol = protocol
    state = await build_state(ctx)
    result: dict[str, Any] = {
        "protocol_id": protocol.id,
        "version": protocol.version,
        "targets": macros_dict(repo.protocol_targets(protocol)),
        "kcal_from_macros": rnd(computed, 0),
        "today_remaining": state_numbers(state)["remaining"],
    }
    if ratio > PROTOCOL_KCAL_TOLERANCE:
        result["note"] = (
            f"kcal {rnd(args.kcal, 0)} vs {rnd(computed, 0)} from 4/4/9 ({ratio * 100:+.0f}%); "
            "check the split"
        )
    log.info("protocol_updated", user_id=ctx.user_id, version=protocol.version)
    return ok(result)


async def set_coaching_intensity(
    ctx: ToolContext, args: schemas.SetCoachingIntensityInput
) -> ToolResult:
    now = ctx.clock.now()
    level = CoachingIntensity(args.level)
    changes: dict[str, Any]
    if args.until is not None:
        until = to_utc(args.until, ctx.tz)
        if until <= now:
            return fail("set_coaching_intensity: 'until' is in the past")
        changes = {"temp_intensity": level, "temp_intensity_until": until}
    else:
        changes = {
            "coaching_intensity": level,
            "temp_intensity": None,
            "temp_intensity_until": None,
        }
    ctx.profile = await repo.upsert_profile(ctx.session, ctx.user_id, changes, now=now)
    base = ctx.profile.coaching_intensity.value
    result: dict[str, Any] = {"level": level.value, "base_level": base}
    if args.until is not None and ctx.profile.temp_intensity_until is not None:
        result["until_local"] = to_local(ctx.profile.temp_intensity_until, ctx.tz).strftime(
            "%Y-%m-%d %H:%M"
        )
        result["restores_to"] = base
    log.info(
        "intensity_set", user_id=ctx.user_id, level=level.value, temporary=args.until is not None
    )
    return ok(result)


async def finish_onboarding(ctx: ToolContext, args: schemas.FinishOnboardingInput) -> ToolResult:
    facts = await _facts(ctx)
    missing = checklist.missing_minimum(ctx.profile, facts)
    if missing:
        return fail(
            "finish_onboarding: missing " + ", ".join(missing) + " — collect these and call again"
        )
    now = ctx.clock.now()
    ctx.profile = await repo.upsert_profile(
        ctx.session,
        ctx.user_id,
        {"onboarding_done_at": now, "onboarding_step": checklist.TOTAL_STEPS},
        now=now,
    )
    await repo.set_user_status(ctx.session, ctx.user_id, UserStatus.active)
    ctx.user.status = UserStatus.active
    lang = resolve_lang(ctx.lang)
    log.info("onboarding_finished", user_id=ctx.user_id)
    return ok(
        {
            "status": "active",
            "done_at": now,
            "send_first": SEND_FIRST[lang],
            "profile": {
                "name": ctx.profile.name,
                "timezone": ctx.user.timezone,
                "kpi": ctx.profile.primary_kpi.value if ctx.profile.primary_kpi else None,
                "intensity": ctx.profile.coaching_intensity.value,
            },
        }
    )


def _integrations(ctx: ToolContext) -> Any:
    registry = ctx.services.get("integrations")
    if registry is not None:
        return registry
    from strikt.events import EventBus
    from strikt.integrations.registry import build_registry

    bind = ctx.session.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("no session factory for the integrations registry")
    return build_registry(ctx.settings, async_sessionmaker(bind), EventBus(), clock=ctx.clock)


async def connect_integration(
    ctx: ToolContext, args: schemas.ConnectIntegrationInput
) -> ToolResult:
    try:
        registry = _integrations(ctx)
    except Exception as exc:
        log.warning("integrations_unavailable", user_id=ctx.user_id, error=repr(exc))
        return fail(f"connect_integration: integrations are not available ({type(exc).__name__})")
    integration = registry.get(args.provider)
    if integration is None:
        return fail(
            f"connect_integration: {args.provider} is not configured on this server "
            "(client id/secret or encryption key missing)"
        )
    try:
        info = await integration.connect(ctx.session, ctx.user)
    except Exception as exc:
        log.warning("connect_failed", user_id=ctx.user_id, provider=args.provider, error=repr(exc))
        return fail(
            f"connect_integration: {args.provider} link could not be built ({type(exc).__name__})"
        )
    result: dict[str, Any] = {
        "provider": info.provider,
        "kind": info.kind,
        "url": info.url,
        "instructions": info.instructions,
    }
    log.info(
        "integration_connect_link", user_id=ctx.user_id, provider=args.provider, kind=info.kind
    )
    return ok(result)


async def import_history(ctx: ToolContext, args: schemas.ImportHistoryInput) -> ToolResult:
    if not args.text.strip():
        return fail("import_history: no rows given")
    result = await importer.import_history(ctx.session, ctx.user, args.text, now=ctx.clock.now())
    if result.total == 0 and not result.duplicates:
        return fail(
            "import_history: nothing imported; "
            + ("; ".join(result.skipped[:5]) or "no rows recognised")
        )
    if result.counts["protocol"]:
        ctx.protocol = await repo.get_active_protocol(ctx.session, ctx.user_id)
    return ok(result.as_dict())
