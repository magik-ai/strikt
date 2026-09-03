"""onboarding.checklist: steps as data, progress, next step, minimum set, deterministic text."""

from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.db import repo
from strikt.db.models import PrimaryKpi, Profile, Protocol, User
from strikt.onboarding import checklist

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def test_steps_are_the_ten_from_the_brief() -> None:
    assert [s.id for s in checklist.STEPS] == list(range(1, 11))
    assert [s.key for s in checklist.STEPS] == [
        "identity",
        "goal",
        "body",
        "schedule",
        "training",
        "food",
        "health",
        "protocol",
        "style",
        "close",
    ]
    fields = {f for s in checklist.STEPS for f in s.fields}
    for name in ("name", "timezone", "goal_text", "primary_kpi", "height_cm", "wake_time", "likes"):
        assert name in fields
    assert set(checklist.MINIMUM) <= fields | {"weight", "protocol"}


def test_empty_profile_starts_at_identity() -> None:
    facts = checklist.Facts()
    statuses = checklist.progress(None, facts)
    assert all(not s.done for s in statuses)
    assert statuses[0].missing == ("name", "timezone")
    assert checklist.next_step(None, facts) is not None
    assert checklist.next_step(None, facts).key == "identity"  # type: ignore[union-attr]
    assert checklist.missing_minimum(None, facts) == list(checklist.MINIMUM)
    assert not checklist.is_complete(None, facts)


def test_utc_timezone_counts_as_unknown() -> None:
    assert not checklist.timezone_known("UTC")
    assert not checklist.timezone_known(None)
    assert checklist.timezone_known("Asia/Dubai")


async def test_progress_from_fields_and_marks(
    session: AsyncSession, user: User, profile: Profile, protocol: Protocol
) -> None:
    facts = checklist.facts_for(user, protocol, has_weight=True)
    statuses = checklist.progress(profile, facts)
    assert all(s.done for s in statuses)  # the fixture user is fully onboarded
    assert checklist.is_complete(profile, facts)
    assert checklist.next_step(profile, facts) is None

    # a profile filled by data but never marked: steps complete from fields
    fresh, _ = await repo.get_or_create_user(
        session, telegram_id=42, chat_id=42, now=NOW, timezone="Asia/Dubai"
    )
    row = await repo.upsert_profile(
        session,
        fresh.id,
        {
            "name": "Anna",
            "goal_text": "lose 5 kg",
            "primary_kpi": PrimaryKpi.weight,
            "height_cm": 170,
            "wake_time": time(7, 0),
            "bed_time": time(23, 0),
        },
        now=NOW,
    )
    facts = checklist.facts_for(fresh, None, has_weight=False)
    by_key = {s.step.key: s for s in checklist.progress(row, facts)}
    assert by_key["identity"].done and by_key["goal"].done and by_key["schedule"].done
    assert not by_key["body"].done and by_key["body"].missing == ("weight",)
    assert not by_key["protocol"].done and not by_key["style"].done
    assert checklist.next_step(row, facts).key == "body"  # type: ignore[union-attr]
    assert checklist.missing_minimum(row, facts) == ["weight", "protocol"]

    # the model marks steps: style has defaults, so only the mark can complete it
    row.onboarding_step = 9
    by_key = {s.step.key: s for s in checklist.progress(row, facts)}
    assert by_key["style"].done and by_key["training"].done
    assert not by_key["close"].done  # close is only done by finish_onboarding


def test_render_state_is_deterministic_and_bilingual() -> None:
    facts = checklist.Facts(timezone="Asia/Dubai")
    en = checklist.render_state(None, "en", facts)
    assert en == checklist.render_state(None, "en-US", facts)
    assert en.startswith("Onboarding:\n[ ] 1. identity")
    assert "(missing: name)" in en and en.endswith("Continue from step 1 (identity).")
    ru = checklist.render_state(None, "ru", facts)
    assert ru.startswith("Онбординг:") and "не хватает: name" in ru
    assert "Продолжай с шага 1" in ru
