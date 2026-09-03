"""Pydantic input models for every tool (PLAN §6.4).

Each model's docstring is the tool description the model sees; each field description is the
parameter doc. Keep them factual and short — they are part of the cached prompt prefix.
Avoid free-form ``dict`` fields and numeric constraints: strict tool use forbids
``additionalProperties`` other than ``false`` and ``minimum``/``maximum``-style keywords.
"""

from __future__ import annotations

from datetime import date as date_, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from strikt.core.types import (
    DataSourceName,
    DayFlagName,
    FoodItemIn,
    FoodSource,
    LabMarker,
    Macros,
    MealSlotName,
    MeasurementTypeName,
)

TOOL_NAMES: tuple[str, ...] = (
    "search_food",
    "log_meal",
    "update_meal",
    "delete_meal",
    "undo_last",
    "log_workout",
    "log_sleep",
    "log_measurement",
    "ingest_lab_report",
    "get_day_state",
    "get_history",
    "search_history",
    "update_profile",
    "update_protocol",
    "set_reminder",
    "cancel_reminder",
    "write_note",
    "retire_note",
    "set_day_flag",
    "set_day_plan",
    "close_day",
    "web_research",
    "render_day_card",
    "connect_integration",
    "set_coaching_intensity",
    "finish_onboarding",
    "import_history",
)
"""Every tool from PLAN §6.4; ``build_registry`` must register exactly this set."""


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------------------- food


class SearchFoodInput(ToolInput):
    """Look up a food in the cache, Open Food Facts (barcode) or USDA (generic). Returns hits
    with per-100 g macros, a serving size when known and a source URL. Use it before estimating
    a packaged or generic product yourself; for restaurant dishes prefer web_research."""

    name: str = Field(description="Food name as written on the label/menu, e.g. 'Greek yogurt 0%'.")
    brand: str | None = Field(default=None, description="Brand, if any.")
    restaurant: str | None = Field(
        default=None, description="Restaurant or delivery kitchen, if this is a menu item."
    )
    barcode: str | None = Field(default=None, description="EAN/UPC digits from the label.")


class MealItemInput(ToolInput):
    """One food line to log. Give final numbers for the portion actually eaten."""

    name: str = Field(description="Item name, e.g. 'chicken shawarma taco'.")
    brand: str | None = Field(default=None, description="Brand, if a packaged product.")
    restaurant: str | None = Field(default=None, description="Restaurant, if a menu item.")
    quantity: float | None = Field(default=None, description="Count of units (e.g. 2 eggs).")
    unit: str | None = Field(default=None, description="Unit for quantity: 'piece', 'cup', 'ml'.")
    grams: float | None = Field(default=None, description="Portion weight in grams, if known.")
    kcal: float = Field(description="Energy for this portion, kcal.")
    protein_g: float = Field(description="Protein for this portion, g.")
    carbs_g: float = Field(description="Carbohydrates for this portion, g.")
    fat_g: float = Field(description="Fat for this portion, g.")
    fiber_g: float = Field(default=0, description="Fiber for this portion, g (0 if none).")
    sodium_mg: float | None = Field(default=None, description="Sodium, mg, when known.")
    alcohol_g: float = Field(default=0, description="Alcohol, g (7 kcal/g), if any.")
    confidence: float = Field(
        default=0.7, description="0..1 confidence in the numbers (label=0.95, photo guess=0.5)."
    )
    source: FoodSource = Field(
        default="model",
        description="Where the numbers came from: label, off, usda, web, model (your estimate) or user.",
    )
    source_url: str | None = Field(default=None, description="URL that backs the numbers.")
    countable: bool = Field(
        default=True,
        description="False for loose foods (pasta, rice, sauces, soups, dressed salads) — the "
        "sanity layer adds the under-report buffer to them.",
    )

    def to_food_item(self) -> FoodItemIn:
        return FoodItemIn(
            name=self.name,
            brand=self.brand,
            restaurant=self.restaurant,
            quantity=self.quantity,
            unit=self.unit,
            grams=self.grams,
            macros=Macros(
                kcal=self.kcal,
                protein_g=self.protein_g,
                carbs_g=self.carbs_g,
                fat_g=self.fat_g,
                fiber_g=self.fiber_g,
                sodium_mg=self.sodium_mg,
                alcohol_g=self.alcohol_g,
            ),
            confidence=self.confidence,
            source=self.source,
            source_url=self.source_url,
            countable=self.countable,
        )


class LogMealInput(ToolInput):
    """Log a meal with one or more items. Runs sanity checks (kcal vs 4/4/9, implausible fiber
    or fat, loose-food buffer, sodium) and returns the meal id, per-item macros after
    corrections with any flags, the day's totals and what remains against the protocol. Log
    first, then reply with the numbers; the user corrects afterwards."""

    items: list[MealItemInput] = Field(description="Items eaten, in the order shown/described.")
    slot: MealSlotName | None = Field(
        default=None,
        description="breakfast/lunch/dinner/snack. Omit when unclear; the user gets a slot button.",
    )
    eaten_at: datetime | None = Field(
        default=None, description="When it was eaten (ISO 8601 with offset). Omit for 'now'."
    )
    note: str | None = Field(default=None, description="Short context, e.g. 'Kinoya, ramen'.")


class MealItemChanges(ToolInput):
    """Fields to change on an item or a meal; only set what changes."""

    name: str | None = Field(default=None, description="New item name.")
    grams: float | None = Field(default=None, description="New portion weight, g.")
    quantity: float | None = Field(default=None, description="New unit count.")
    unit: str | None = Field(default=None, description="New unit.")
    kcal: float | None = Field(default=None, description="New kcal for the portion.")
    protein_g: float | None = Field(default=None, description="New protein, g.")
    carbs_g: float | None = Field(default=None, description="New carbs, g.")
    fat_g: float | None = Field(default=None, description="New fat, g.")
    fiber_g: float | None = Field(default=None, description="New fiber, g.")
    sodium_mg: float | None = Field(default=None, description="New sodium, mg.")
    countable: bool | None = Field(default=None, description="Set loose/countable.")
    slot: MealSlotName | None = Field(default=None, description="Move the meal to this slot.")
    eaten_at: datetime | None = Field(default=None, description="New eaten-at time.")
    note: str | None = Field(default=None, description="New meal note.")


class UpdateMealInput(ToolInput):
    """Correct a logged meal or one of its items ('actually I ate a quarter', 'that was 150 g',
    'salad was 200 kcal'). Give item_id to change an item, or meal_id to change slot/time/note.
    Returns the updated item, the meal and the day totals."""

    meal_id: int | None = Field(default=None, description="Meal to change (slot/time/note).")
    item_id: int | None = Field(default=None, description="Item to change (portion/macros).")
    changes: MealItemChanges = Field(description="The fields that change.")
    reason: str | None = Field(
        default=None, description="One line on why, kept as the user's correction record."
    )


class DeleteMealInput(ToolInput):
    """Remove a logged meal (soft delete; totals are recomputed)."""

    meal_id: int = Field(description="Meal id from log_meal or get_day_state.")


class UndoLastInput(ToolInput):
    """Undo the most recent meal log (same as the Undo button)."""


# ------------------------------------------------------------------------------------ training


class ZoneMinutes(ToolInput):
    """Minutes spent in each heart-rate zone (WHOOP zones 0-5)."""

    z0: float | None = Field(default=None, description="Zone 0 minutes (rest).")
    z1: float | None = Field(default=None, description="Zone 1 minutes.")
    z2: float | None = Field(default=None, description="Zone 2 minutes.")
    z3: float | None = Field(default=None, description="Zone 3 minutes.")
    z4: float | None = Field(default=None, description="Zone 4 minutes.")
    z5: float | None = Field(default=None, description="Zone 5 minutes.")


class LogWorkoutInput(ToolInput):
    """Log a training session from a WHOOP screenshot, a description or a manual entry. Returns
    the stored workout plus the comparison with the last session of the same sport and the
    30-day average (duration, strain, kcal, avg HR), which the reply should comment on."""

    sport: str = Field(description="Activity, e.g. 'run', 'strength', 'cycling', 'walk'.")
    started_at: datetime = Field(description="Start time, ISO 8601 with offset.")
    ended_at: datetime | None = Field(default=None, description="End time, if known.")
    duration_min: float | None = Field(default=None, description="Duration in minutes.")
    strain: float | None = Field(default=None, description="WHOOP strain (0-21), if shown.")
    kcal: float | None = Field(default=None, description="Energy burned, kcal.")
    avg_hr: int | None = Field(default=None, description="Average heart rate, bpm.")
    max_hr: int | None = Field(default=None, description="Max heart rate, bpm.")
    zones_min: ZoneMinutes | None = Field(default=None, description="Minutes per HR zone.")
    distance_m: float | None = Field(default=None, description="Distance in metres.")
    source: DataSourceName = Field(
        default="manual", description="whoop, screenshot, manual, apple_health or other."
    )
    external_id: str | None = Field(
        default=None, description="Provider id for deduplication, when available."
    )
    note: str | None = Field(default=None, description="Short note (e.g. 'heavy squats').")


class SleepStages(ToolInput):
    """Minutes per sleep stage."""

    light: float | None = Field(default=None, description="Light sleep minutes.")
    deep: float | None = Field(default=None, description="Deep (SWS) minutes.")
    rem: float | None = Field(default=None, description="REM minutes.")
    awake: float | None = Field(default=None, description="Awake minutes in bed.")


class LogSleepInput(ToolInput):
    """Log a night of sleep from WHOOP or a description. Returns the stored record plus sleep
    onset versus the agreed bedtime and wake time versus the anchor."""

    started_at: datetime = Field(description="Went to sleep (or to bed), ISO 8601 with offset.")
    ended_at: datetime = Field(description="Woke up, ISO 8601 with offset.")
    in_bed_min: float | None = Field(default=None, description="Time in bed, minutes.")
    asleep_min: float | None = Field(default=None, description="Time asleep, minutes.")
    performance_pct: float | None = Field(default=None, description="WHOOP sleep performance %.")
    stages_min: SleepStages | None = Field(default=None, description="Minutes per stage.")
    respiratory_rate: float | None = Field(default=None, description="Breaths per minute.")
    disturbances: int | None = Field(default=None, description="Number of disturbances.")
    source: DataSourceName = Field(default="manual", description="Data source.")
    external_id: str | None = Field(default=None, description="Provider id for deduplication.")


# ---------------------------------------------------------------------------------------- body


class LogMeasurementInput(ToolInput):
    """Log a body measurement (weight, waist at navel, body fat, blood pressure, steps, RHR,
    HRV). Returns the stored value, the previous reading and the 7-day trend for weight."""

    type: MeasurementTypeName = Field(description="What was measured.")
    value: float = Field(description="Numeric value.")
    unit: str = Field(description="Unit: kg, cm, %, mmHg, steps, bpm, ms.")
    measured_at: datetime | None = Field(
        default=None, description="When it was measured. Omit for 'now'."
    )
    source: str = Field(default="manual", description="manual, scale, withings, apple_health…")
    note: str | None = Field(default=None, description="Context, e.g. 'fasted, after salty day'.")


class IngestLabReportInput(ToolInput):
    """Store structured lab markers you read from a report image or PDF. Do not interpret in the
    tool; the reply references markers only where they change advice."""

    taken_at: date_ = Field(description="Sample date on the report (YYYY-MM-DD).")
    markers: list[LabMarker] = Field(description="Every marker row you could read.")
    source: str | None = Field(default=None, description="Lab name or 'photo'.")


# --------------------------------------------------------------------------------------- state


class GetDayStateInput(ToolInput):
    """Totals, remaining budget, logged items, training, sleep, recovery and due measurements
    for one local date (default today). Use it before any statement about today's numbers."""

    date: date_ | None = Field(default=None, description="Local date; omit for today.")


HistoryKind = Literal[
    "meals", "workouts", "sleep", "recoveries", "measurements", "labs", "notes", "summaries", "days"
]


class GetHistoryInput(ToolInput):
    """Structured history rows: what was eaten on a date, strain trend over a month, weight
    readings, past summaries. Filter by kinds and a date range; add text to narrow by name."""

    kinds: list[HistoryKind] = Field(description="Which tables to read.")
    date_from: date_ | None = Field(default=None, description="Start date (inclusive), local.")
    date_to: date_ | None = Field(default=None, description="End date (inclusive), local.")
    text: str | None = Field(default=None, description="Optional substring filter on names/text.")
    limit: int = Field(default=50, description="Max rows per kind (keep it small).")


class SearchHistoryInput(ToolInput):
    """Full-text search over past conversation, coach notes, summaries and food names. Use it
    for questions about the past that a date range does not answer ('when did I last have
    ramen', 'what did we decide about fat')."""

    text: str = Field(description="Search phrase in the user's language.")
    limit: int = Field(default=20, description="Max hits.")


# ------------------------------------------------------------------------------------- profile


class TrainingPlan(ToolInput):
    """The user's intended training schedule."""

    days: list[str] | None = Field(default=None, description="Weekdays, e.g. ['mon','wed','fri'].")
    sessions_per_week: int | None = Field(default=None, description="Planned sessions per week.")
    kinds: list[str] | None = Field(default=None, description="Kinds: strength, run, swim…")


class ProfileFields(ToolInput):
    """Profile fields to set; only include what the user just told you."""

    name: str | None = Field(default=None, description="How to address the user.")
    language: str | None = Field(default=None, description="BCP-47 reply language, e.g. 'ru'.")
    timezone: str | None = Field(default=None, description="IANA timezone, e.g. 'Asia/Dubai'.")
    city: str | None = Field(default=None, description="City.")
    country: str | None = Field(default=None, description="Country.")
    height_cm: float | None = Field(default=None, description="Height, cm.")
    birth_year: int | None = Field(default=None, description="Year of birth.")
    sex: str | None = Field(default=None, description="male / female / other.")
    goal_text: str | None = Field(default=None, description="Goal in the user's own words.")
    primary_kpi: Literal["waist", "weight", "bodyfat", "performance"] | None = Field(
        default=None, description="The one number that defines progress."
    )
    kpi_target_low: float | None = Field(default=None, description="'Good' target for the KPI.")
    kpi_target_high: float | None = Field(default=None, description="'Excellent' target.")
    kpi_unit: str | None = Field(default=None, description="Unit of the KPI (cm, kg, %).")
    wake_time: time | None = Field(default=None, description="Fixed wake time, HH:MM local.")
    bed_time: time | None = Field(default=None, description="Target bedtime, HH:MM local.")
    work_pattern: str | None = Field(default=None, description="Work schedule in one line.")
    training_plan: TrainingPlan | None = Field(default=None, description="Training schedule.")
    meal_sources: list[str] | None = Field(
        default=None, description="Where meals come from: delivery, home, office, restaurants."
    )
    wearable: str | None = Field(default=None, description="whoop / garmin / apple_watch / none.")
    likes: list[str] | None = Field(default=None, description="Foods the user likes.")
    dislikes: list[str] | None = Field(default=None, description="Foods to stop suggesting.")
    allergies: list[str] | None = Field(default=None, description="Allergies / intolerances.")
    dietary_rules: list[str] | None = Field(default=None, description="halal, vegetarian, …")
    alcohol: str | None = Field(default=None, description="Alcohol habits in one line.")
    sweet_tooth: str | None = Field(default=None, description="Sweet cravings in one line.")
    comfort_food: str | None = Field(default=None, description="What comfort food means.")
    health_context: str | None = Field(
        default=None, description="Conditions, doctor's instructions, relevant lab context."
    )
    medications: str | None = Field(default=None, description="Medications / supplements.")
    coaching_intensity: Literal["gentle", "direct", "pushy", "drill_sergeant"] | None = Field(
        default=None, description="How blunt the coach is. Default pushy."
    )
    explanation_level: Literal["short", "full"] | None = Field(
        default=None, description="short (default) or full explanations."
    )
    proactive_enabled: bool | None = Field(default=None, description="Allow check-ins.")
    quiet_start: time | None = Field(default=None, description="Quiet hours start, HH:MM.")
    quiet_end: time | None = Field(default=None, description="Quiet hours end, HH:MM.")
    checkin_times: list[str] | None = Field(
        default=None, description="Preferred check-in times, HH:MM local."
    )
    waist_cadence_days: int | None = Field(default=None, description="Waist every N days (14).")
    weight_cadence_days: int | None = Field(default=None, description="Weight every N days (7).")
    onboarding_step: int | None = Field(
        default=None, description="Highest onboarding step completed (1-10)."
    )


class UpdateProfileInput(ToolInput):
    """Save what the user told you about themselves (onboarding or later). Also marks the
    onboarding step when relevant. Returns the updated profile and remaining onboarding steps."""

    fields: ProfileFields = Field(description="Only the fields that change.")


class UpdateProtocolInput(ToolInput):
    """Set the active macro scheme (a new version; the old one is kept). Call it when the user
    picks or changes targets. Returns the new protocol and the remaining budget for today."""

    kcal: float = Field(description="Daily energy target, kcal.")
    protein_g: float = Field(description="Daily protein, g.")
    fat_g: float = Field(description="Daily fat, g.")
    carbs_g: float = Field(description="Daily carbs, g.")
    fiber_g: float = Field(description="Daily fiber target, g.")
    rationale: str = Field(description="One or two lines on why these numbers.")


class SetReminderInput(ToolInput):
    """Schedule a one-off reminder the coach will send at a time ('ask me at 8 about waist')."""

    when: datetime = Field(description="When to send, ISO 8601 with offset.")
    text: str = Field(description="What to remind, in the user's language.")
    kind: str | None = Field(default=None, description="measurement, meal, bedtime, custom.")


class CancelReminderInput(ToolInput):
    """Cancel a pending reminder by id."""

    id: int = Field(description="Reminder id.")


# -------------------------------------------------------------------------------------- memory

NoteKindName = Literal["preference", "pattern", "health", "rule", "event", "answer", "commitment"]


class WriteNoteInput(ToolInput):
    """Remember a durable observation: a preference ('dislikes chia'), a pattern ('one meal until
    evening → overeats'), a health fact, a rule the user set, a planned event, an answer to
    'why did you disappear', or a commitment. Notes are shown to you every turn."""

    kind: NoteKindName = Field(description="Note category.")
    text: str = Field(description="One sentence, specific, in the user's language.")
    confidence: float = Field(default=0.8, description="0..1 how sure you are.")
    expires_at: datetime | None = Field(
        default=None, description="For temporary facts (a trip, a temporary intensity)."
    )
    supersedes_id: int | None = Field(default=None, description="Id of the note this one replaces.")


class RetireNoteInput(ToolInput):
    """Deactivate a note that is no longer true."""

    id: int = Field(description="Note id.")


# ----------------------------------------------------------------------------------------- day


class SetDayFlagInput(ToolInput):
    """Mark a day as salty, alcohol, travel, sick, planned_indulgence or off. Flags change what
    the coach says the next morning ('don't weigh, it's water') and pause targets when sick."""

    date: date_ = Field(description="Local date.")
    flag: DayFlagName = Field(description="Which flag.")
    on: bool = Field(default=True, description="Set (true) or clear (false).")


class DayPlan(ToolInput):
    """The day's commitment, as agreed in the morning or around a known event."""

    breakfast: str | None = Field(default=None, description="What and roughly when.")
    lunch: str | None = Field(default=None, description="What and roughly when.")
    dinner: str | None = Field(default=None, description="What and roughly when.")
    snacks: str | None = Field(default=None, description="Planned snacks, if any.")
    training: str | None = Field(default=None, description="Planned session and time.")
    events: list[str] | None = Field(default=None, description="Known events (dinner out…).")
    bedtime: str | None = Field(default=None, description="Agreed bedtime, HH:MM.")
    notes: str | None = Field(default=None, description="Anything else agreed.")


class SetDayPlanInput(ToolInput):
    """Store the day's plan (the morning commitment) so deviations can be pointed out later."""

    date: date_ = Field(description="Local date.")
    plan: DayPlan = Field(description="The plan.")


class CloseDayInput(ToolInput):
    """Close the day: writes the day summary and your verdict, triggers the weekly summary
    update. Returns final totals to state in the close message (with the bed line)."""

    date: date_ = Field(description="Local date to close (usually today).")
    verdict: str = Field(description="One-line verdict: numbers first, one thing to fix tomorrow.")


# ------------------------------------------------------------------------------------ research


class WebResearchInput(ToolInput):
    """Research on the web with citations: restaurant menu macros, product labels, food safety,
    anything not in the food databases. Returns {answer, sources} or {error}. When it fails, say
    'couldn't verify, estimating from ingredients' and estimate yourself."""

    query: str = Field(description="What to find, specific: 'Kinoya Dubai tonkotsu ramen macros'.")
    urls: list[str] | None = Field(
        default=None, description="URLs from the conversation to fetch (menus, PDFs)."
    )


class RenderDayCardInput(ToolInput):
    """Return the Today card text exactly as pinned in the chat."""


class ConnectIntegrationInput(ToolInput):
    """Get the connection link or instructions for WHOOP (OAuth), Withings (OAuth) or Apple
    Health (Shortcuts webhook). Send the link to the user as-is."""

    provider: Literal["whoop", "withings", "apple_health"] = Field(description="Provider.")


class SetCoachingIntensityInput(ToolInput):
    """Change how hard the coach pushes, optionally until a date ('ease off this week'). The
    previous level is restored automatically afterwards."""

    level: Literal["gentle", "direct", "pushy", "drill_sergeant"] = Field(description="New level.")
    until: datetime | None = Field(default=None, description="Restore the previous level after.")


class FinishOnboardingInput(ToolInput):
    """Mark onboarding complete. Fails with the list of missing items if the minimum set (name,
    timezone, height, weight, goal, KPI, wake/bed times, protocol) is not stored yet."""


class ImportHistoryInput(ToolInput):
    """Import pasted or forwarded summaries of past days: meals, workouts, measurements and
    preferences you extracted, written as rows with source=imported. Returns counts per kind."""

    text: str = Field(
        description="Structured rows, one per line, as described in the import instructions."
    )


SCHEMAS: dict[str, type[ToolInput]] = {
    "search_food": SearchFoodInput,
    "log_meal": LogMealInput,
    "update_meal": UpdateMealInput,
    "delete_meal": DeleteMealInput,
    "undo_last": UndoLastInput,
    "log_workout": LogWorkoutInput,
    "log_sleep": LogSleepInput,
    "log_measurement": LogMeasurementInput,
    "ingest_lab_report": IngestLabReportInput,
    "get_day_state": GetDayStateInput,
    "get_history": GetHistoryInput,
    "search_history": SearchHistoryInput,
    "update_profile": UpdateProfileInput,
    "update_protocol": UpdateProtocolInput,
    "set_reminder": SetReminderInput,
    "cancel_reminder": CancelReminderInput,
    "write_note": WriteNoteInput,
    "retire_note": RetireNoteInput,
    "set_day_flag": SetDayFlagInput,
    "set_day_plan": SetDayPlanInput,
    "close_day": CloseDayInput,
    "web_research": WebResearchInput,
    "render_day_card": RenderDayCardInput,
    "connect_integration": ConnectIntegrationInput,
    "set_coaching_intensity": SetCoachingIntensityInput,
    "finish_onboarding": FinishOnboardingInput,
    "import_history": ImportHistoryInput,
}
