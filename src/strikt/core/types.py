"""Pydantic value types shared by the nutrition engine, the agent tools and the Telegram layer.

These are *views* and *inputs*; the ORM rows live in ``strikt.db.models``.
"""

from __future__ import annotations

from datetime import date as date_, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FoodSource = Literal["label", "off", "usda", "web", "model", "user"]
MealSlotName = Literal["breakfast", "lunch", "dinner", "snack", "unknown"]
MealSourceName = Literal["photo", "text", "voice", "imported", "planned", "forwarded"]
DataSourceName = Literal["whoop", "screenshot", "manual", "apple_health", "other"]
MeasurementTypeName = Literal[
    "weight", "waist", "bodyfat", "bp_sys", "bp_dia", "steps", "rhr", "hrv", "other"
]
DayFlagName = Literal["salty", "alcohol", "travel", "sick", "planned_indulgence", "off"]
Severity = Literal["info", "warn", "error"]
AttachmentKind = Literal["image", "document", "voice", "link"]


class Macros(BaseModel):
    """Energy and macronutrients for one item, one meal or one day."""

    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float = 0
    sodium_mg: float | None = None
    alcohol_g: float = 0

    @classmethod
    def zero(cls) -> Macros:
        return cls(kcal=0, protein_g=0, carbs_g=0, fat_g=0, fiber_g=0, sodium_mg=None, alcohol_g=0)

    def __add__(self, other: Macros) -> Macros:
        sodium: float | None
        if self.sodium_mg is None and other.sodium_mg is None:
            sodium = None
        else:
            sodium = (self.sodium_mg or 0) + (other.sodium_mg or 0)
        return Macros(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
            fiber_g=self.fiber_g + other.fiber_g,
            sodium_mg=sodium,
            alcohol_g=self.alcohol_g + other.alcohol_g,
        )

    def scaled(self, factor: float) -> Macros:
        return Macros(
            kcal=self.kcal * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
            fiber_g=self.fiber_g * factor,
            sodium_mg=None if self.sodium_mg is None else self.sodium_mg * factor,
            alcohol_g=self.alcohol_g * factor,
        )


class FoodItemIn(BaseModel):
    """One food line as it enters the log (from the model, a label, a DB hit or the user)."""

    name: str
    brand: str | None = None
    restaurant: str | None = None
    quantity: float | None = None
    unit: str | None = None
    grams: float | None = None
    macros: Macros
    confidence: float = 0.7
    source: FoodSource = "model"
    source_url: str | None = None
    countable: bool = True


class FoodHit(BaseModel):
    """A resolved food from the cache / Open Food Facts / USDA / web research."""

    name: str
    brand: str | None = None
    restaurant: str | None = None
    barcode: str | None = None
    per_100g: Macros
    serving_g: float | None = None
    serving_desc: str | None = None
    source: FoodSource
    source_url: str | None = None
    confidence: float = 0.8


class LabMarker(BaseModel):
    """One lab-report row (the model reads the image; this is what it stores)."""

    marker: str
    value: float
    unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None
    flag: str | None = None


class DayTotals(BaseModel):
    macros: Macros
    items: int = 0
    meals: int = 0


class Remaining(BaseModel):
    """Target minus totals; negative means over."""

    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float

    @classmethod
    def from_targets(cls, targets: Macros, totals: Macros) -> Remaining:
        return cls(
            kcal=targets.kcal - totals.kcal,
            protein_g=targets.protein_g - totals.protein_g,
            carbs_g=targets.carbs_g - totals.carbs_g,
            fat_g=targets.fat_g - totals.fat_g,
            fiber_g=targets.fiber_g - totals.fiber_g,
        )


class Flag(BaseModel):
    """A sanity-check finding on a food item (see PLAN §5 ``sanity.py``)."""

    code: str
    severity: Severity = "warn"
    message: str
    corrected: Macros | None = None
    needs_health_context: bool = False


class Button(BaseModel):
    """One inline keyboard button; exactly one of ``callback_data`` / ``url`` is set."""

    text: str
    callback_data: str | None = None
    url: str | None = None


class MealItemView(BaseModel):
    id: int
    name: str
    grams: float | None = None
    macros: Macros
    countable: bool = True
    confidence: float = 0.7
    flags: list[str] = Field(default_factory=list)


class MealView(BaseModel):
    id: int
    slot: MealSlotName = "unknown"
    logged_at: datetime
    eaten_at: datetime | None = None
    items: list[MealItemView] = Field(default_factory=list)
    macros: Macros
    note: str | None = None


class WorkoutView(BaseModel):
    id: int
    sport: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_min: float | None = None
    strain: float | None = None
    kcal: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    zones_min: dict[str, float] | None = None
    source: DataSourceName = "manual"


class SleepView(BaseModel):
    started_at: datetime
    ended_at: datetime
    in_bed_min: float | None = None
    asleep_min: float | None = None
    performance_pct: float | None = None


class RecoveryView(BaseModel):
    date: date_
    score: float | None = None
    rhr: float | None = None
    hrv_ms: float | None = None
    spo2: float | None = None


class DayState(BaseModel):
    """Everything the card, the context block and the verify step need about one local day."""

    date: date_
    totals: DayTotals
    targets: Macros
    remaining: Remaining
    meals: list[MealView] = Field(default_factory=list)
    workouts: list[WorkoutView] = Field(default_factory=list)
    sleep: SleepView | None = None
    recovery: RecoveryView | None = None
    measurements_due: list[str] = Field(default_factory=list)
    closed: bool = False
    flags: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    verdict: str | None = None


class Attachment(BaseModel):
    kind: AttachmentKind
    file_id: str | None = None
    mime: str | None = None
    bytes_b64: str | None = None
    text: str | None = None
    sha256: str | None = None
    filename: str | None = None


class Incoming(BaseModel):
    """A normalised inbound Telegram message (text, photo album, voice, document, link)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: int
    chat_id: int
    message_id: int
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    forwarded_from: str | None = None
    received_at: datetime


class Outgoing(BaseModel):
    """One reply to send. ``text`` is Telegram HTML."""

    text: str
    keyboard: list[list[Button]] | None = None
    reply_to: int | None = None
