"""The onboarding interview as data: brief §4's ten steps, which profile fields each fills,
what is required before ``finish_onboarding`` may succeed, and a deterministic rendering for
the system block (PLAN §6.2: the profile block is cached, so the text must be byte-stable for
the same profile).

A step counts as done when the model marked it (``profile.onboarding_step >= step.id``) or when
every required field for it is already filled - a user who pastes their whole story in one
message does not get asked again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.db.models import Profile, Protocol, User

TOTAL_STEPS = 10
UNSET_TIMEZONE = "UTC"


@dataclass(frozen=True)
class Step:
    """One interview step: the profile fields it fills and the ones that make it done."""

    id: int
    key: str
    fields: tuple[str, ...]
    required: tuple[str, ...]
    hint_en: str
    hint_ru: str

    def hint(self, lang: str | None) -> str:
        return self.hint_ru if resolve_lang(lang) == "ru" else self.hint_en


STEPS: tuple[Step, ...] = (
    Step(
        1,
        "identity",
        ("name", "language", "timezone", "city", "country"),
        ("name", "timezone"),
        "name, city → timezone",
        "имя, город → часовой пояс",
    ),
    Step(
        2,
        "goal",
        (
            "goal_text",
            "primary_kpi",
            "kpi_target_low",
            "kpi_target_high",
            "kpi_unit",
            "waist_cadence_days",
            "weight_cadence_days",
        ),
        ("goal_text", "primary_kpi"),
        "goal in their words; propose one KPI with good/excellent targets and a cadence",
        "цель их словами; предложить один KPI с целями «хорошо/отлично» и частотой замеров",
    ),
    Step(
        3,
        "body",
        ("height_cm", "birth_year", "sex", "weight", "waist"),
        ("height_cm", "weight"),
        "height, weight, waist, age, sex; log weight and waist with log_measurement",
        "рост, вес, талия, возраст, пол; вес и талию записать через log_measurement",
    ),
    Step(
        4,
        "schedule",
        ("wake_time", "bed_time", "work_pattern", "training_plan", "meal_sources"),
        ("wake_time", "bed_time"),
        "wake and bed times (wake is the anchor), work pattern, training days, where meals come from",
        "подъём и отбой (подъём - якорь), режим работы, дни тренировок, откуда еда",
    ),
    Step(
        5,
        "training",
        ("training_plan", "wearable"),
        ("wearable",),
        "what and how often; wearable - offer connect_integration right there",
        "что и как часто; трекер - сразу предложить connect_integration",
    ),
    Step(
        6,
        "food",
        (
            "likes",
            "dislikes",
            "allergies",
            "dietary_rules",
            "alcohol",
            "sweet_tooth",
            "comfort_food",
        ),
        ("food_any",),
        "likes, dislikes, allergies, dietary rules, alcohol, sweet tooth, comfort food",
        "любит, не любит, аллергии, правила питания, алкоголь, тяга к сладкому, комфортная еда",
    ),
    Step(
        7,
        "health",
        ("health_context", "medications"),
        ("health_context",),
        "conditions, labs to consider (ingest_lab_report), medications, doctor's instructions",
        "диагнозы, анализы (ingest_lab_report), лекарства, указания врача",
    ),
    Step(
        8,
        "protocol",
        ("protocol",),
        ("protocol",),
        "propose kcal and macros with 2–3 alternatives; store the pick with update_protocol",
        "предложить калории и макросы с 2–3 вариантами; выбор сохранить через update_protocol",
    ),
    Step(
        9,
        "style",
        (
            "coaching_intensity",
            "explanation_level",
            "proactive_enabled",
            "checkin_times",
            "quiet_start",
            "quiet_end",
        ),
        ("style_marked",),
        "bluntness (default pushy), explanation length (short), check-ins and quiet hours",
        "жёсткость (по умолчанию pushy), длина объяснений (short), чек-ины и тихие часы",
    ),
    Step(
        10,
        "close",
        ("onboarding_done_at",),
        ("onboarding_done_at",),
        "summarise the profile, ask for corrections, call finish_onboarding, say what to send first",
        "подвести итог, спросить про правки, вызвать finish_onboarding, сказать, что прислать первым",
    ),
)
STEP_BY_KEY: dict[str, Step] = {step.key: step for step in STEPS}

MINIMUM: tuple[str, ...] = (
    "name",
    "timezone",
    "height_cm",
    "weight",
    "goal_text",
    "primary_kpi",
    "wake_time",
    "bed_time",
    "protocol",
)
"""Brief §4 / PLAN §10: what must exist before ``finish_onboarding`` succeeds."""


@dataclass(frozen=True)
class StepStatus:
    step: Step
    done: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    """What the checklist knows besides the profile row."""

    timezone: str | None = None
    has_weight: bool = False
    has_protocol: bool = False


def facts_for(user: User | None, protocol: Protocol | None, *, has_weight: bool = False) -> Facts:
    return Facts(
        timezone=user.timezone if user is not None else None,
        has_weight=has_weight,
        has_protocol=protocol is not None,
    )


def timezone_known(tz: str | None) -> bool:
    """Users are created with ``UTC``; the interview replaces it with a real zone."""
    return bool(tz) and tz != UNSET_TIMEZONE


def _has(profile: Profile | None, field: str, facts: Facts) -> bool:
    if field == "timezone":
        return timezone_known(facts.timezone)
    if field == "weight":
        return facts.has_weight
    if field == "protocol":
        return facts.has_protocol
    if profile is None:
        return False
    if field == "food_any":
        return any(
            bool(getattr(profile, name, None))
            for name in ("likes", "dislikes", "allergies", "dietary_rules", "alcohol")
        )
    if field == "style_marked":
        return bool(profile.checkin_times) or profile.onboarding_step >= STEP_BY_KEY["style"].id
    value: Any = getattr(profile, field, None)
    if isinstance(value, str):
        return bool(value.strip())
    if value is None:
        return False
    if isinstance(value, list | dict):
        return len(value) > 0
    return True


def missing_fields(profile: Profile | None, step: Step, facts: Facts) -> tuple[str, ...]:
    return tuple(field for field in step.required if not _has(profile, field, facts))


def progress(profile: Profile | None, facts: Facts | None = None) -> list[StepStatus]:
    """Done/pending per step, in order."""
    facts = facts or Facts()
    marked = profile.onboarding_step if profile is not None else 0
    out: list[StepStatus] = []
    for step in STEPS:
        missing = missing_fields(profile, step, facts)
        done = marked >= step.id or not missing
        if step.key == "close":
            done = profile is not None and profile.onboarding_done_at is not None
        out.append(StepStatus(step=step, done=done, missing=missing if not done else ()))
    return out


def next_step(profile: Profile | None, facts: Facts | None = None) -> Step | None:
    for status in progress(profile, facts):
        if not status.done:
            return status.step
    return None


def missing_minimum(profile: Profile | None, facts: Facts) -> list[str]:
    """Names from :data:`MINIMUM` that are still empty."""
    return [field for field in MINIMUM if not _has(profile, field, facts)]


def is_complete(profile: Profile | None, facts: Facts) -> bool:
    return not missing_minimum(profile, facts)


def render_state(profile: Profile | None, lang: str | None, facts: Facts | None = None) -> str:
    """Deterministic checklist text for the system block (same profile → identical bytes)."""
    ru = resolve_lang(lang) == "ru"
    statuses = progress(profile, facts)
    lines = ["Онбординг:" if ru else "Onboarding:"]
    for status in statuses:
        mark = "[x]" if status.done else "[ ]"
        line = f"{mark} {status.step.id}. {status.step.key} - {status.step.hint(lang)}"
        if status.missing:
            label = "не хватает" if ru else "missing"
            line += f" ({label}: {', '.join(status.missing)})"
        lines.append(line)
    pending = next_step(profile, facts)
    if pending is None:
        lines.append("Все шаги пройдены." if ru else "All steps done.")
    else:
        lines.append(
            f"Продолжай с шага {pending.id} ({pending.key})."
            if ru
            else f"Continue from step {pending.id} ({pending.key})."
        )
    return "\n".join(lines)
