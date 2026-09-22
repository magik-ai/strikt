"""The eval harness: seed a database, run one real turn against it, grade what happened.

Why it exists (docs/AGENT.md, "Known gaps"): every claim about this agent so far is a measurement
of the *request* - tokens, tool counts - not of the answers. This runs the real turn loop
(``agent.loop.run_turn``) against a real model with a per-case SQLite database, and grades the end
state the way the agent-eval guidance says to: what is in the database afterwards, which tools ran,
what the reply says. Not a transcript vibe check.

One case = a seeded day + one incoming message + the expectations. Cases live in
``evals/cases.json`` so they can be read and extended without touching Python.

Grading is programmatic by default. ``expect.judge`` adds one rubric question answered by a
second, cheaper model; it is off unless ``--judge`` is passed, because it costs money and the
programmatic checks carry the headline metric.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import LLMClient, LLMResult, LLMUsage
from strikt.agent.loop import TurnDeps, run_turn
from strikt.agent.tools import Registry, build_registry
from strikt.config import Settings
from strikt.core.clock import FakeClock, to_local
from strikt.core.types import FoodItemIn, Incoming, Macros
from strikt.db import repo
from strikt.db.engine import SQLITE_MEMORY_URL, init_sqlite_for_tests, make_engine
from strikt.db.models import (
    CoachingIntensity,
    Meal,
    MealItem,
    MealSlot,
    Measurement,
    Note,
    PrimaryKpi,
    Reminder,
    TurnRole,
    User,
    UserStatus,
    Workout,
)

CASES_PATH = Path(__file__).resolve().parent / "cases.json"
TELEGRAM_ID = 424_242
DEFAULT_TZ = "Asia/Dubai"


# ------------------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    id: str
    tags: list[str]
    message: str
    expect: dict[str, Any]
    now: str
    seed: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.now)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [Case(**row) for row in raw["cases"]]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case ids")
    return cases


# ------------------------------------------------------------------------------------ seeding


def _time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _at(day: date, hhmm: str, tz: str) -> datetime:
    from zoneinfo import ZoneInfo

    naive = datetime.combine(day, _time(hhmm))
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


async def seed_user(session: AsyncSession, case: Case, now: datetime) -> User:
    """The seeded owner: a finished onboarding unless the case says otherwise."""
    seed = case.seed
    tz = str(seed.get("timezone", DEFAULT_TZ))
    user, _ = await repo.get_or_create_user(
        session,
        telegram_id=TELEGRAM_ID,
        chat_id=TELEGRAM_ID,
        now=now,
        language=str(seed.get("language", "ru")),
        timezone=tz,
        status=UserStatus.active,
    )
    profile: dict[str, Any] = {
        "name": "Owner",
        "city": "Dubai",
        "height_cm": 190,
        "birth_year": 1988,
        "sex": "male",
        "goal_text": "waist down, protein up",
        "primary_kpi": PrimaryKpi.waist,
        "kpi_target_low": 94,
        "kpi_target_high": 90,
        "kpi_unit": "cm",
        "coaching_intensity": CoachingIntensity.pushy,
        "onboarding_step": 10,
        "onboarding_done_at": now,
    }
    for key, value in dict(seed.get("profile", {})).items():
        profile[key] = _time(value) if key in {"wake_time", "bed_time"} else value
    if seed.get("onboarding_unfinished"):
        profile["onboarding_step"] = 2
        profile["onboarding_done_at"] = None
    await repo.upsert_profile(session, user.id, profile, now=now)

    targets = dict(seed.get("targets", {}))
    await repo.set_active_protocol(
        session,
        user.id,
        kcal=float(targets.get("kcal", 2085)),
        protein_g=float(targets.get("protein_g", 210)),
        fat_g=float(targets.get("fat_g", 105)),
        carbs_g=float(targets.get("carbs_g", 75)),
        fiber_g=float(targets.get("fiber_g", 25)),
        rationale="seeded",
        now=now,
    )
    await session.commit()
    return user


async def seed_day(session: AsyncSession, user: User, case: Case, now: datetime) -> None:
    """Meals (today and older), conversation rows and an open proactive send."""
    tz = user.timezone or DEFAULT_TZ
    today = to_local(now, tz).date()
    for meal in list(case.seed.get("meals", [])):
        day = today - timedelta(days=int(meal.get("days_ago", 0)))
        eaten = _at(day, str(meal.get("at", "13:00")), tz)
        items = [
            FoodItemIn(
                name=str(item["name"]),
                grams=item.get("grams"),
                macros=Macros(
                    kcal=float(item.get("kcal", 0)),
                    protein_g=float(item.get("protein_g", 0)),
                    carbs_g=float(item.get("carbs_g", 0)),
                    fat_g=float(item.get("fat_g", 0)),
                    fiber_g=float(item.get("fiber_g", 0)),
                ),
                countable=bool(item.get("countable", True)),
            )
            for item in meal["items"]
        ]
        await repo.add_meal_with_items(
            session,
            user.id,
            day_date=day,
            items=items,
            slot=MealSlot(str(meal.get("slot", "unknown"))),
            logged_at=eaten,
            eaten_at=eaten,
        )
        await repo.get_or_open_day(session, user.id, day, now=eaten)

    minutes = 90
    for row in list(case.seed.get("history", [])):
        minutes -= 5
        await repo.add_turn(
            session,
            user.id,
            role=TurnRole(str(row["role"])),
            content=[{"type": "text", "text": str(row["text"])}],
            now=now - timedelta(minutes=max(1, minutes)),
        )
    send = case.seed.get("proactive_send")
    if send:
        await repo.add_proactive_send(
            session,
            user.id,
            trigger=str(send.get("trigger", "no_dinner")),
            window_key=f"{send.get('trigger', 'no_dinner')}:{today}",
            step=int(send.get("step", 1)),
            sent_at=now - timedelta(minutes=int(send.get("minutes_ago", 10))),
            text=str(send["text"]),
        )
        await repo.add_turn(
            session,
            user.id,
            role=TurnRole.assistant,
            content=[{"type": "text", "text": str(send["text"])}],
            now=now - timedelta(minutes=int(send.get("minutes_ago", 10))),
        )
    for flag in list(case.seed.get("day_flags", [])):
        await repo.set_day_flag(session, user.id, today, str(flag), True, now=now)
    await session.commit()


# ------------------------------------------------------------------------------- end state


@dataclass
class Snapshot:
    """What the database held, so the checks can diff it. Items carry their day: rewriting a
    closed day is the failure that started this work, and every case checks for it."""

    items: dict[int, tuple[str, float, float, date]]
    meals: int
    workouts: int
    measurements: int
    reminders: int
    notes: int
    profile: dict[str, str]

    @staticmethod
    async def take(session: AsyncSession, user_id: int) -> Snapshot:
        rows = (
            await session.execute(
                select(MealItem, Meal.day_date)
                .join(Meal, Meal.id == MealItem.meal_id)
                .where(MealItem.user_id == user_id)
            )
        ).all()
        counts: dict[str, int] = {}
        for model in (Meal, Workout, Measurement, Reminder, Note):
            counts[model.__name__] = int(
                await session.scalar(
                    select(func.count()).select_from(model).where(model.user_id == user_id)
                )
                or 0
            )
        profile = await repo.get_profile(session, user_id)
        fields = {}
        if profile is not None:
            for name in ("wake_time", "bed_time", "name", "city", "goal_text"):
                value = getattr(profile, name, None)
                fields[name] = "" if value is None else str(value)
        return Snapshot(
            items={
                row[0].id: (
                    row[0].name,
                    float(row[0].kcal or 0),
                    float(row[0].fiber_g or 0),
                    row[1],
                )
                for row in rows
            },
            meals=counts["Meal"],
            workouts=counts["Workout"],
            measurements=counts["Measurement"],
            reminders=counts["Reminder"],
            notes=counts["Note"],
            profile=fields,
        )


# ------------------------------------------------------------------------------------ running


class RecordingLLM:
    """Wraps the real client so the trace keeps every request and response of the turn."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []
        self.usage = LLMUsage()
        self.cost_usd = 0.0
        self.model = getattr(inner, "model", "unknown")

    async def message(self, **kwargs: Any) -> LLMResult:
        result: LLMResult = await self._inner.message(**kwargs)
        self.calls.append(
            {
                "purpose": str(kwargs.get("purpose")),
                "tools": [t.get("name") for t in (kwargs.get("tools") or [])],
                "messages": [dict(m) for m in kwargs.get("messages") or []],
                "system": kwargs.get("system"),
                "content": result.content,
                "stop_reason": result.stop_reason,
            }
        )
        self.usage = self.usage + result.usage
        self.cost_usd += result.cost_usd
        self.model = result.model or self.model
        return result


@dataclass
class RunOutcome:
    case: Case
    reply: str
    tools_used: list[str]
    rounds: int
    before: Snapshot
    after: Snapshot
    usage: LLMUsage
    cost_usd: float
    model: str
    latency_s: float
    trace: list[dict[str, Any]]
    error: str | None = None


async def run_case(case: Case, llm: LLMClient, settings: Settings) -> RunOutcome:
    """One case end to end on its own in-memory database."""
    import time as _time

    engine = make_engine(SQLITE_MEMORY_URL)
    await init_sqlite_for_tests(engine)
    from strikt.db.engine import make_session_factory

    factory = make_session_factory(engine)
    recorder = RecordingLLM(llm)
    started = _time.perf_counter()
    try:
        async with factory() as session:
            now = case.when.astimezone(UTC)
            clock = FakeClock(now)
            user = await seed_user(session, case, now)
            await seed_day(session, user, case, now)
            before = await Snapshot.take(session, user.id)
            registry: Registry = build_registry()
            deps = TurnDeps(
                session=session,
                user=user,
                llm=recorder,
                registry=registry,
                clock=clock,
                settings=settings,
            )
            incoming = Incoming(
                user_id=user.telegram_id,
                chat_id=user.chat_id,
                message_id=1,
                text=case.message,
                received_at=now,
            )
            result = await run_turn(deps, incoming)
            after = await Snapshot.take(session, user.id)
    finally:
        await engine.dispose()
    return RunOutcome(
        case=case,
        reply=result.text,
        tools_used=list(result.tools_used),
        rounds=result.rounds,
        before=before,
        after=after,
        usage=recorder.usage,
        cost_usd=recorder.cost_usd,
        model=recorder.model,
        latency_s=round(_time.perf_counter() - started, 2),
        trace=build_trace(recorder, result.text),
        error=result.error,
    )


def build_trace(recorder: RecordingLLM, reply: str) -> list[dict[str, Any]]:
    """The turn as the report format wants it: system, user, tool calls, results, reply."""
    turns: list[dict[str, Any]] = []
    calls = [c for c in recorder.calls if c["purpose"] == "turn"]
    if not calls:
        return [{"role": "assistant", "content": reply}]
    first = calls[0]
    system = first.get("system")
    if isinstance(system, str):
        turns.append({"role": "system", "content": system})
    elif isinstance(system, list):
        turns.append(
            {"role": "system", "content": "\n\n".join(str(b.get("text", "")) for b in system)}
        )
    turns.extend(
        {"role": str(message["role"]), "content": _flatten(message["content"])}
        for message in first["messages"]
    )
    turns.extend(
        {
            "role": "tool_call",
            "name": str(block.get("name")),
            "content": json.dumps(block.get("input", {}), ensure_ascii=False, indent=2),
        }
        for call in calls
        for block in call["content"]
        if block.get("type") == "tool_use"
    )
    turns.append({"role": "assistant", "content": reply})
    return turns


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(f"[tool_result] {_flatten(block.get('content'))}")
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type')}]")
        return "\n".join(parts)
    return str(content)


# ------------------------------------------------------------------------------------ grading


@dataclass
class Grade:
    passed: bool
    failures: list[str]
    checks: int


_NUMBER = re.compile(r"\d")


def grade(outcome: RunOutcome) -> Grade:
    """Every expectation of the case, checked against the end state. All must hold."""
    expect = outcome.case.expect
    failures: list[str] = []
    checks = 0
    used = outcome.tools_used
    before, after = outcome.before, outcome.after

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(f"{name}: {detail}")

    if outcome.error:
        failures.append(f"turn_error: {outcome.error}")

    for tool in expect.get("must_call", []):
        check("must_call", tool in used, f"{tool} not called (called: {used or 'nothing'})")
    for tool in expect.get("must_not_call", []):
        check("must_not_call", tool not in used, f"{tool} was called")
    if "must_call_any" in expect:
        options = list(expect["must_call_any"])
        check(
            "must_call_any",
            any(tool in used for tool in options),
            f"none of {options} was called (called: {used or 'nothing'})",
        )

    if "meals_added" in expect:
        want = int(expect["meals_added"])
        got = after.meals - before.meals
        check("meals_added", got == want, f"expected {want}, got {got}")
    if "items_added" in expect:
        want = int(expect["items_added"])
        got = len(after.items) - len(before.items)
        check("items_added", got == want, f"expected {want}, got {got}")

    new_items = {i: v for i, v in after.items.items() if i not in before.items}
    if "fiber_added_at_least" in expect:
        want = float(expect["fiber_added_at_least"])
        got = sum(fiber for _, _, fiber, _ in new_items.values())
        check("fiber_added_at_least", got >= want, f"expected >= {want} g, got {got:g} g")
    if "kcal_added_between" in expect:
        low, high = (float(v) for v in expect["kcal_added_between"])
        got = sum(kcal for _, kcal, _, _ in new_items.values())
        check("kcal_added_between", low <= got <= high, f"expected {low}-{high}, got {got:g}")

    for name, count in dict(expect.get("rows_added", {})).items():
        got = getattr(after, name) - getattr(before, name)
        check("rows_added", got == int(count), f"{name}: expected {count}, got {got}")

    # Always, in every case: a turn may not rewrite a day that is already closed.
    today = to_local(outcome.case.when, DEFAULT_TZ).date()
    stale = [
        f"#{i} {before.items[i][0]} ({before.items[i][3]})"
        for i in before.items
        if before.items[i][3] < today and before.items[i] != after.items.get(i)
    ]
    check("older_days_untouched", not stale, f"changed rows from earlier days: {stale}")
    if expect.get("today_untouched", False):
        changed = [
            f"#{i} {before.items[i][0]}"
            for i in before.items
            if before.items[i] != after.items.get(i)
        ]
        check("today_untouched", not changed, f"changed rows: {changed}")

    for field_name, want_value in dict(expect.get("profile_equals", {})).items():
        got_value = after.profile.get(field_name, "")
        check(
            "profile_equals",
            got_value.startswith(str(want_value)),
            f"{field_name}: expected {want_value}, got {got_value or 'nothing'}",
        )

    for pattern in expect.get("reply_matches", []):
        check(
            "reply_matches",
            bool(re.search(pattern, outcome.reply, re.IGNORECASE)),
            f"/{pattern}/ not in the reply",
        )
    for pattern in expect.get("reply_not_matches", []):
        check(
            "reply_not_matches",
            not re.search(pattern, outcome.reply, re.IGNORECASE),
            f"/{pattern}/ is in the reply",
        )
    if expect.get("reply_has_number"):
        check("reply_has_number", bool(_NUMBER.search(outcome.reply)), "no number in the reply")
    if "max_rounds" in expect:
        want = int(expect["max_rounds"])
        check("max_rounds", outcome.rounds <= want, f"{outcome.rounds} rounds, allowed {want}")

    return Grade(passed=not failures, failures=failures, checks=checks)
