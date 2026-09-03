"""Profile tools (update_profile, update_protocol, intensity, finish_onboarding,
connect_integration, import_history) and web_research degradation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import STOP_END_TURN, FakeLLM, LLMResult
from strikt.agent.tools import ToolContext, profile as profile_tools, research, schemas
from strikt.agent.usage import LLMUsage
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.models import CoachingIntensity, MealSource, User, UserStatus
from strikt.telegram.messenger import FakeMessenger

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def parsed(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(str(result.content))
    return data


async def fresh_ctx(
    session: AsyncSession, clock: FakeClock, settings: Settings, fake_llm: FakeLLM
) -> ToolContext:
    """A brand-new user in onboarding: no profile, no protocol, timezone still UTC."""
    user, _ = await repo.get_or_create_user(
        session, telegram_id=555, chat_id=555, now=clock.now(), language="en"
    )
    await session.commit()
    return ToolContext(
        session=session,
        user=user,
        profile=None,
        protocol=None,
        clock=clock,
        settings=settings,
        services={"llm": fake_llm, "messenger": FakeMessenger()},
    )


# ---------------------------------------------------------------------------- update_profile


async def test_update_profile_saves_fields_and_marks_steps(
    session: AsyncSession, clock: FakeClock, settings: Settings, fake_llm: FakeLLM
) -> None:
    ctx = await fresh_ctx(session, clock, settings, fake_llm)
    result = parsed(
        await profile_tools.update_profile(
            ctx,
            schemas.UpdateProfileInput(
                fields=schemas.ProfileFields(
                    name="Ilya",
                    language="ru",
                    timezone="Asia/Dubai",
                    city="Dubai",
                    onboarding_step=1,
                )
            ),
        )
    )
    assert set(result["saved"]) == {"language", "timezone", "city", "name"}
    assert ctx.user.timezone == "Asia/Dubai" and ctx.user.language == "ru"
    assert ctx.profile is not None and ctx.profile.onboarding_step == 1
    assert result["onboarding"]["next_step"] == "2 goal"
    assert "timezone" not in result["onboarding"]["missing_minimum"]
    assert "[x] 1. identity" in result["checklist"]
    refreshed = await repo.get_user(session, ctx.user_id)
    assert refreshed is not None and refreshed.timezone == "Asia/Dubai"


async def test_update_profile_rejects_bad_timezone_and_empty(
    session: AsyncSession, clock: FakeClock, settings: Settings, fake_llm: FakeLLM
) -> None:
    ctx = await fresh_ctx(session, clock, settings, fake_llm)
    bad = await profile_tools.update_profile(
        ctx, schemas.UpdateProfileInput(fields=schemas.ProfileFields(timezone="Dubai/Marina"))
    )
    assert bad.is_error and "IANA" in str(bad.content)
    empty = await profile_tools.update_profile(
        ctx, schemas.UpdateProfileInput(fields=schemas.ProfileFields())
    )
    assert empty.is_error


async def test_update_profile_enums_training_plan_and_step_never_regresses(
    tool_ctx: ToolContext,
) -> None:
    result = parsed(
        await profile_tools.update_profile(
            tool_ctx,
            schemas.UpdateProfileInput(
                fields=schemas.ProfileFields(
                    primary_kpi="weight",
                    coaching_intensity="gentle",
                    training_plan=schemas.TrainingPlan(days=["mon", "thu"], sessions_per_week=2),
                    dislikes=["chia pudding"],
                    onboarding_step=3,
                )
            ),
        )
    )
    assert tool_ctx.profile is not None
    assert tool_ctx.profile.coaching_intensity is CoachingIntensity.gentle
    assert tool_ctx.profile.training_plan == {"days": ["mon", "thu"], "sessions_per_week": 2}
    assert tool_ctx.profile.onboarding_step == 10  # already finished: never regresses
    assert result["onboarding"]["done"] is True and "checklist" not in result


# --------------------------------------------------------------------------- update_protocol


async def test_update_protocol_versions_and_returns_remaining(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    result = parsed(
        await profile_tools.update_protocol(
            tool_ctx,
            schemas.UpdateProtocolInput(
                kcal=1900,
                protein_g=180,
                fat_g=100,
                carbs_g=70,
                fiber_g=28,
                rationale="hungry on 105 F",
            ),
        )
    )
    assert result["version"] == 2
    assert result["targets"] == {"kcal": 1900, "P": 180, "C": 70, "F": 100, "fiber": 28}
    assert result["today_remaining"]["kcal"] == 1900
    assert "note" not in result  # 180*4+70*4+100*9 = 1900 exactly
    protocols = await repo.list_protocols(session, tool_ctx.user_id)
    assert [(p.version, p.active) for p in protocols] == [(1, False), (2, True)]
    assert tool_ctx.protocol is not None and tool_ctx.protocol.version == 2
    off = parsed(
        await profile_tools.update_protocol(
            tool_ctx,
            schemas.UpdateProtocolInput(
                kcal=2500, protein_g=180, fat_g=100, carbs_g=70, fiber_g=28, rationale="x"
            ),
        )
    )
    assert off["version"] == 3 and "check the split" in off["note"]
    bad = await profile_tools.update_protocol(
        tool_ctx,
        schemas.UpdateProtocolInput(
            kcal=0, protein_g=1, fat_g=1, carbs_g=1, fiber_g=0, rationale=""
        ),
    )
    assert bad.is_error


# ----------------------------------------------------------------------- coaching intensity


async def test_set_coaching_intensity_temporary_and_permanent(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    temp = parsed(
        await profile_tools.set_coaching_intensity(
            tool_ctx,
            schemas.SetCoachingIntensityInput(level="gentle", until=datetime(2026, 9, 10, 9, 0)),
        )
    )
    assert temp["level"] == "gentle" and temp["restores_to"] == "pushy"
    assert temp["until_local"] == "2026-09-10 09:00"
    assert tool_ctx.profile is not None
    assert tool_ctx.profile.temp_intensity is CoachingIntensity.gentle
    assert tool_ctx.profile.coaching_intensity is CoachingIntensity.pushy
    permanent = parsed(
        await profile_tools.set_coaching_intensity(
            tool_ctx, schemas.SetCoachingIntensityInput(level="drill_sergeant")
        )
    )
    assert permanent["base_level"] == "drill_sergeant"
    stored = await repo.get_profile(session, tool_ctx.user_id)
    assert stored is not None and stored.temp_intensity is None
    assert stored.temp_intensity_until is None
    past = await profile_tools.set_coaching_intensity(
        tool_ctx, schemas.SetCoachingIntensityInput(level="gentle", until=NOW)
    )
    assert past.is_error


# ------------------------------------------------------------------------ finish_onboarding


async def test_finish_onboarding_refuses_until_minimum_set_exists(
    session: AsyncSession, clock: FakeClock, settings: Settings, fake_llm: FakeLLM
) -> None:
    ctx = await fresh_ctx(session, clock, settings, fake_llm)
    refused = await profile_tools.finish_onboarding(ctx, schemas.FinishOnboardingInput())
    assert refused.is_error
    for name in ("name", "timezone", "height_cm", "weight", "goal_text", "primary_kpi", "protocol"):
        assert name in str(refused.content)

    await profile_tools.update_profile(
        ctx,
        schemas.UpdateProfileInput(
            fields=schemas.ProfileFields(
                name="Ilya",
                height_cm=190,
                goal_text="waist under 94",
                primary_kpi="waist",
                wake_time=time(8, 0),
                bed_time=time(0, 30),
            )
        ),
    )
    await repo.add_measurement(
        session, ctx.user_id, type="weight", value=104, unit="kg", measured_at=clock.now()
    )
    await profile_tools.update_protocol(
        ctx,
        schemas.UpdateProtocolInput(
            kcal=2000, protein_g=210, fat_g=105, carbs_g=75, fiber_g=30, rationale="chosen"
        ),
    )
    still = await profile_tools.finish_onboarding(ctx, schemas.FinishOnboardingInput())
    assert still.is_error and str(still.content).endswith("collect these and call again")
    assert "timezone" in str(still.content) and "name" not in str(still.content)

    await profile_tools.update_profile(
        ctx, schemas.UpdateProfileInput(fields=schemas.ProfileFields(timezone="Asia/Dubai"))
    )
    done = parsed(await profile_tools.finish_onboarding(ctx, schemas.FinishOnboardingInput()))
    assert done["status"] == "active" and "Send your next meal" in done["send_first"]
    assert done["profile"]["timezone"] == "Asia/Dubai" and done["profile"]["kpi"] == "waist"
    user = await repo.get_user(session, ctx.user_id)
    assert user is not None and user.status is UserStatus.active
    assert ctx.profile is not None and ctx.profile.onboarding_done_at is not None
    assert ctx.profile.onboarding_step == 10


# ------------------------------------------------------------------------ connect_integration


async def test_connect_integration_apple_health_builds_webhook_link(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    result = parsed(
        await profile_tools.connect_integration(
            tool_ctx, schemas.ConnectIntegrationInput(provider="apple_health")
        )
    )
    assert result["provider"] == "apple_health" and result["kind"] == "webhook"
    assert result["url"].startswith("http://localhost:8080/webhooks/apple")
    row = await repo.get_integration(session, tool_ctx.user_id, "apple_health")
    assert row is not None and row.webhook_token and row.webhook_token in result["url"]


async def test_connect_integration_unconfigured_provider(tool_ctx: ToolContext) -> None:
    tool_ctx.services["integrations"] = {}
    result = await profile_tools.connect_integration(
        tool_ctx, schemas.ConnectIntegrationInput(provider="whoop")
    )
    assert result.is_error and "not configured" in str(result.content)


# ----------------------------------------------------------------------------- import_history


async def test_import_history_writes_rows_with_source_imported(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    text = """
meal | 2026-08-14 | 13:20 | lunch | Kinoya tonkotsu ramen | kcal=780 p=38 c=85 f=30 fiber=4 | loose
workout | 2026-08-14 | 18:30 | strength | duration=62 strain=9.4 kcal=410 avg_hr=118 max_hr=156
measurement | 2026-08-18 | waist | 103 | cm
note | preference | dislikes chia pudding; eats it only for fiber
garbage line
"""
    result = parsed(
        await profile_tools.import_history(tool_ctx, schemas.ImportHistoryInput(text=text))
    )
    assert result["imported"] == {
        "meals": 1,
        "workouts": 1,
        "sleep": 0,
        "measurements": 1,
        "labs": 0,
        "notes": 1,
        "protocol": 0,
    }
    assert result["skipped_total"] == 1 and "unknown row kind" in result["skipped"][0]
    meals = await repo.list_meals_for_date(session, tool_ctx.user_id, datetime(2026, 8, 14).date())
    assert len(meals) == 1 and meals[0].source is MealSource.imported
    assert meals[0].items[0].countable is False
    waist = await repo.latest_by_type(session, tool_ctx.user_id, "waist")
    assert waist is not None and waist.source == "imported"
    nothing = await profile_tools.import_history(
        tool_ctx, schemas.ImportHistoryInput(text="nonsense | 1 | 2")
    )
    assert nothing.is_error


# -------------------------------------------------------------------------------- web_research


def research_result() -> LLMResult:
    return LLMResult(
        content=[
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
                "input": {"query": "Kinoya tonkotsu ramen macros"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://kinoya.ae/menu",
                        "title": "Kinoya menu",
                        "encrypted_content": "abc",
                    },
                    {
                        "type": "web_search_result",
                        "url": "https://example.com/other",
                        "title": "Other",
                        "encrypted_content": "def",
                    },
                ],
            },
            {
                "type": "text",
                "text": "Tonkotsu ramen: 780 kcal, 38 P, 85 C, 30 F per bowl.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://kinoya.ae/menu",
                        "title": "Kinoya menu",
                        "cited_text": "780 kcal",
                    }
                ],
            },
        ],
        stop_reason=STOP_END_TURN,
        usage=LLMUsage(input_tokens=10, output_tokens=5),
        model="fake-model",
    )


async def test_web_research_returns_answer_and_sources(
    tool_ctx: ToolContext, fake_llm: FakeLLM
) -> None:
    fake_llm.queue(research_result())
    result = parsed(
        await research.web_research(
            tool_ctx,
            schemas.WebResearchInput(
                query="Kinoya Dubai tonkotsu ramen macros", urls=["https://kinoya.ae/menu"]
            ),
        )
    )
    assert result["answer"].startswith("Tonkotsu ramen: 780 kcal")
    assert result["sources"] == ["https://kinoya.ae/menu", "https://example.com/other"]
    assert result["searches"] == 1 and result["verified"] is True
    call = fake_llm.calls[0]
    assert call["purpose"] == "research" and call["effort"] is None  # EFFORT_RESEARCH applies
    assert [t["type"] for t in call["tools"]] == ["web_search_20260318", "web_fetch_20260318"]
    assert result["untrusted"] is True
    assert "untrusted data" in research.SYSTEM_PROMPT
    tool_ctx.settings.web_search_tool_type = "web_search_20260209"
    assert research.research_tools(tool_ctx.settings)[0]["type"] == "web_search_20260209"
    assert tool_ctx.settings.effort_research == "low"
    assert call["tools"][0]["max_uses"] == 5 and call["tools"][1]["max_uses"] == 3
    assert "https://kinoya.ae/menu" in call["messages"][0]["content"][0]["text"]
    assert call["cache_tail"] is False


async def test_web_research_degrades_when_llm_raises_or_refuses(
    tool_ctx: ToolContext, fake_llm: FakeLLM
) -> None:
    # no scripted response → FakeLLM raises → honest one-liner, never an exception
    failed = await research.web_research(tool_ctx, schemas.WebResearchInput(query="anything"))
    assert failed.is_error and str(failed.content).startswith("couldn't verify")
    fake_llm.queue(FakeLLM.refusal())
    refused = await research.web_research(tool_ctx, schemas.WebResearchInput(query="anything"))
    assert refused.is_error and "refused" in str(refused.content)
    fake_llm.queue(FakeLLM.text(""))
    empty = await research.web_research(tool_ctx, schemas.WebResearchInput(query="anything"))
    assert empty.is_error and "no answer" in str(empty.content)
    blank = await research.web_research(tool_ctx, schemas.WebResearchInput(query="  "))
    assert blank.is_error
    tool_ctx.services.pop("llm")
    unwired = await research.web_research(tool_ctx, schemas.WebResearchInput(query="x"))
    assert unwired.is_error and "not wired" in str(unwired.content)


async def test_web_research_follows_pause_turn(tool_ctx: ToolContext, fake_llm: FakeLLM) -> None:
    paused = LLMResult(
        content=[{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}],
        stop_reason="pause_turn",
        usage=LLMUsage(),
        model="fake-model",
    )
    fake_llm.queue(paused, research_result())
    result = parsed(await research.web_research(tool_ctx, schemas.WebResearchInput(query="q")))
    assert result["searches"] == 2
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[1]["messages"][1]["role"] == "assistant"


def test_extract_sources_handles_errors_and_fetch_blocks() -> None:
    content: list[dict[str, Any]] = [
        {
            "type": "web_search_tool_result",
            "tool_use_id": "x",
            "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"},
        },
        {
            "type": "web_fetch_tool_result",
            "tool_use_id": "y",
            "content": {"type": "web_fetch_result", "url": "https://a.example/menu.pdf"},
        },
        {"type": "text", "text": "…"},
    ]
    assert research.extract_sources(content) == ["https://a.example/menu.pdf"]
    assert research.search_errors(content) == ["max_uses_exceeded"]
    assert research.count_searches(content) == 0


def test_user_fixture_is_not_reused_by_fresh_ctx(user: User) -> None:
    assert user.telegram_id != 555
