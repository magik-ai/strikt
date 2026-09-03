"""SQLAlchemy 2.0 typed models — every table from PLAN §3.

Conventions:
- Every table except ``users``, ``invites`` and ``foods`` carries ``user_id`` and every query
  filters by it (see ``repo.py``).
- Timestamps are UTC ``DateTime(timezone=True)``; "local date" columns are ``Date`` computed in
  the user's timezone. SQLite returns naive datetimes: normalise with ``core.clock.ensure_utc``.
- IDs are ``BigInteger`` with a SQLite ``Integer`` variant so autoincrement works in tests.
- Enums are Python ``StrEnum`` stored as ``VARCHAR`` (``native_enum=False``), never Postgres
  ENUM types, so migrations stay trivial and SQLite tests are faithful.
- JSON is ``sa.JSON`` with a ``JSONB`` variant on Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --------------------------------------------------------------------------------------- enums


class UserStatus(StrEnum):
    invited = "invited"
    language = "language"
    onboarding = "onboarding"
    active = "active"
    paused = "paused"
    deleted = "deleted"


class PrimaryKpi(StrEnum):
    waist = "waist"
    weight = "weight"
    bodyfat = "bodyfat"
    performance = "performance"


class CoachingIntensity(StrEnum):
    gentle = "gentle"
    direct = "direct"
    pushy = "pushy"
    drill_sergeant = "drill_sergeant"


class ExplanationLevel(StrEnum):
    short = "short"
    full = "full"


class MealSlot(StrEnum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    snack = "snack"
    unknown = "unknown"


class MealSource(StrEnum):
    photo = "photo"
    text = "text"
    voice = "voice"
    imported = "imported"
    planned = "planned"
    forwarded = "forwarded"


class ItemSource(StrEnum):
    label = "label"
    off = "off"
    usda = "usda"
    web = "web"
    model = "model"
    user = "user"


class DataSource(StrEnum):
    """Source of workouts / sleep / recoveries."""

    whoop = "whoop"
    screenshot = "screenshot"
    manual = "manual"
    apple_health = "apple_health"
    other = "other"


class MeasurementType(StrEnum):
    weight = "weight"
    waist = "waist"
    bodyfat = "bodyfat"
    bp_sys = "bp_sys"
    bp_dia = "bp_dia"
    steps = "steps"
    rhr = "rhr"
    hrv = "hrv"
    other = "other"


class NoteKind(StrEnum):
    preference = "preference"
    pattern = "pattern"
    health = "health"
    rule = "rule"
    event = "event"
    answer = "answer"
    commitment = "commitment"


class SecretService(StrEnum):
    """The optional keys a user can hand over in the chat. The Anthropic key is not here:
    it is required, and it lives on ``users`` (``llm_key_enc``)."""

    openai = "openai"  # voice notes get transcribed
    usda = "usda"  # the food database answers faster and more often


class ReminderStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    cancelled = "cancelled"
    #: Came due, the attempt did not end in a message. Never retried: see
    #: ``ProactiveScheduler._run_reminder_checks``.
    missed = "missed"


class TurnRole(StrEnum):
    user = "user"
    assistant = "assistant"


class SummaryKind(StrEnum):
    day = "day"
    week = "week"


class Provider(StrEnum):
    whoop = "whoop"
    withings = "withings"
    apple_health = "apple_health"


class IntegrationStatus(StrEnum):
    pending = "pending"
    connected = "connected"
    expired = "expired"
    revoked = "revoked"
    error = "error"


class UsagePurpose(StrEnum):
    turn = "turn"
    verify = "verify"
    proactive = "proactive"
    summary = "summary"
    research = "research"
    transcribe = "transcribe"


class DayFlag(StrEnum):
    salty = "salty"
    alcohol = "alcohol"
    travel = "travel"
    sick = "sick"
    planned_indulgence = "planned_indulgence"
    off = "off"


ALL_ENUMS: tuple[type[StrEnum], ...] = (
    UserStatus,
    PrimaryKpi,
    CoachingIntensity,
    ExplanationLevel,
    MealSlot,
    MealSource,
    ItemSource,
    DataSource,
    MeasurementType,
    NoteKind,
    ReminderStatus,
    SecretService,
    TurnRole,
    SummaryKind,
    Provider,
    IntegrationStatus,
    UsagePurpose,
    DayFlag,
)

# --------------------------------------------------------------------------------------- types

BigIntPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")
TZDateTime = sa.DateTime(timezone=True)


def enum_col(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """A portable string enum column (VARCHAR + Python-side validation)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


def user_fk() -> sa.ForeignKey:
    return sa.ForeignKey("users.id", ondelete="CASCADE")


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------- users


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        enum_col(UserStatus, "user_status"), nullable=False, default=UserStatus.invited
    )
    language: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    invite_code: Mapped[str | None] = mapped_column(sa.String(32))
    # Bring-your-own-key: the user's Anthropic API key, Fernet-encrypted with
    # TOKEN_ENCRYPTION_KEY (never the plaintext), its last four characters for the "ends in …"
    # line, and when it was (re)set. Every model call for this user is billed to this key.
    llm_key_enc: Mapped[str | None] = mapped_column(sa.Text)
    llm_key_last4: Mapped[str | None] = mapped_column(sa.String(4))
    llm_key_set_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # The optional key the coach just asked for, if any. A USDA key is forty plain characters
    # and matches nothing on sight, so the next message is read as one only while this is set.
    awaiting_secret: Mapped[str | None] = mapped_column(sa.String(16))

    profile: Mapped[Profile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class Profile(Base):
    """1:1 with ``users``; everything onboarding collects (brief §4)."""

    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(BigIntPK, user_fk(), primary_key=True)
    name: Mapped[str | None] = mapped_column(sa.String(120))
    city: Mapped[str | None] = mapped_column(sa.String(120))
    country: Mapped[str | None] = mapped_column(sa.String(120))
    height_cm: Mapped[float | None] = mapped_column(sa.Float)
    birth_year: Mapped[int | None] = mapped_column(sa.Integer)
    sex: Mapped[str | None] = mapped_column(sa.String(16))
    goal_text: Mapped[str | None] = mapped_column(sa.Text)
    primary_kpi: Mapped[PrimaryKpi | None] = mapped_column(enum_col(PrimaryKpi, "primary_kpi"))
    kpi_target_low: Mapped[float | None] = mapped_column(sa.Float)
    kpi_target_high: Mapped[float | None] = mapped_column(sa.Float)
    kpi_unit: Mapped[str | None] = mapped_column(sa.String(16))
    wake_time: Mapped[time | None] = mapped_column(sa.Time)
    bed_time: Mapped[time | None] = mapped_column(sa.Time)
    work_pattern: Mapped[str | None] = mapped_column(sa.Text)
    training_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    meal_sources: Mapped[list[Any] | None] = mapped_column(JSONType)
    wearable: Mapped[str | None] = mapped_column(sa.String(64))
    likes: Mapped[list[Any] | None] = mapped_column(JSONType)
    dislikes: Mapped[list[Any] | None] = mapped_column(JSONType)
    allergies: Mapped[list[Any] | None] = mapped_column(JSONType)
    dietary_rules: Mapped[list[Any] | None] = mapped_column(JSONType)
    alcohol: Mapped[str | None] = mapped_column(sa.Text)
    sweet_tooth: Mapped[str | None] = mapped_column(sa.Text)
    comfort_food: Mapped[str | None] = mapped_column(sa.Text)
    health_context: Mapped[str | None] = mapped_column(sa.Text)
    medications: Mapped[str | None] = mapped_column(sa.Text)
    coaching_intensity: Mapped[CoachingIntensity] = mapped_column(
        enum_col(CoachingIntensity, "coaching_intensity"),
        nullable=False,
        default=CoachingIntensity.pushy,
    )
    explanation_level: Mapped[ExplanationLevel] = mapped_column(
        enum_col(ExplanationLevel, "explanation_level"),
        nullable=False,
        default=ExplanationLevel.short,
    )
    proactive_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    quiet_start: Mapped[time] = mapped_column(sa.Time, nullable=False, default=time(0, 0))
    quiet_end: Mapped[time] = mapped_column(sa.Time, nullable=False, default=time(7, 30))
    checkin_times: Mapped[list[Any] | None] = mapped_column(JSONType)
    temp_intensity: Mapped[CoachingIntensity | None] = mapped_column(
        enum_col(CoachingIntensity, "coaching_intensity")
    )
    temp_intensity_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    onboarding_step: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    onboarding_done_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    waist_cadence_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=14)
    weight_cadence_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=7)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class Protocol(Base):
    """Versioned macro scheme; exactly one active per user."""

    __tablename__ = "protocols"
    __table_args__ = (sa.Index("ix_protocols_user_active", "user_id", "active"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kcal: Mapped[float] = mapped_column(sa.Float, nullable=False)
    protein_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    fat_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    carbs_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    fiber_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


# ---------------------------------------------------------------------------------------- days


class Day(Base):
    __tablename__ = "days"
    __table_args__ = (sa.UniqueConstraint("user_id", "date", name="uq_days_user_date"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    verdict: Mapped[str | None] = mapped_column(sa.Text)
    card_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    flags: Mapped[list[Any] | None] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(sa.Text)


# --------------------------------------------------------------------------------------- meals


class Meal(Base):
    __tablename__ = "meals"
    __table_args__ = (
        sa.Index("ix_meals_user_day", "user_id", "day_date"),
        sa.Index("ix_meals_user_logged", "user_id", "logged_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    day_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    slot: Mapped[MealSlot] = mapped_column(
        enum_col(MealSlot, "meal_slot"), nullable=False, default=MealSlot.unknown
    )
    logged_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    eaten_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    source: Mapped[MealSource] = mapped_column(
        enum_col(MealSource, "meal_source"), nullable=False, default=MealSource.text
    )
    raw_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    note: Mapped[str | None] = mapped_column(sa.Text)
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    items: Mapped[list[MealItem]] = relationship(
        back_populates="meal",
        cascade="all, delete-orphan",
        order_by="MealItem.position",
    )


class MealItem(Base):
    __tablename__ = "meal_items"
    __table_args__ = (sa.Index("ix_meal_items_user_meal", "user_id", "meal_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("meals.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(sa.String(120))
    restaurant: Mapped[str | None] = mapped_column(sa.String(120))
    quantity: Mapped[float | None] = mapped_column(sa.Float)
    unit: Mapped[str | None] = mapped_column(sa.String(32))
    grams: Mapped[float | None] = mapped_column(sa.Float)
    kcal: Mapped[float] = mapped_column(sa.Float, nullable=False)
    protein_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    carbs_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    fat_g: Mapped[float] = mapped_column(sa.Float, nullable=False)
    fiber_g: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    sodium_mg: Mapped[float | None] = mapped_column(sa.Float)
    alcohol_g: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.7)
    source: Mapped[ItemSource] = mapped_column(
        enum_col(ItemSource, "item_source"), nullable=False, default=ItemSource.model
    )
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    countable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    model_estimate: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    user_correction: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    flags: Mapped[list[Any] | None] = mapped_column(JSONType)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    meal: Mapped[Meal] = relationship(back_populates="items")


class Food(Base):
    """Shared (cross-user) cache of resolved foods."""

    __tablename__ = "foods"
    __table_args__ = (sa.Index("ix_foods_barcode", "barcode"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(sa.String(400), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(sa.String(120))
    restaurant: Mapped[str | None] = mapped_column(sa.String(120))
    barcode: Mapped[str | None] = mapped_column(sa.String(64))
    per_100g: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    serving_g: Mapped[float | None] = mapped_column(sa.Float)
    serving_desc: Mapped[str | None] = mapped_column(sa.String(120))
    source: Mapped[ItemSource] = mapped_column(enum_col(ItemSource, "item_source"), nullable=False)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.8)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


# ------------------------------------------------------------------------------------ training


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_workouts_external"),
        sa.Index("ix_workouts_user_started", "user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    source: Mapped[DataSource] = mapped_column(enum_col(DataSource, "data_source"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    sport: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    duration_min: Mapped[float | None] = mapped_column(sa.Float)
    strain: Mapped[float | None] = mapped_column(sa.Float)
    kcal: Mapped[float | None] = mapped_column(sa.Float)
    avg_hr: Mapped[int | None] = mapped_column(sa.Integer)
    max_hr: Mapped[int | None] = mapped_column(sa.Integer)
    zones_min: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    distance_m: Mapped[float | None] = mapped_column(sa.Float)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class Sleep(Base):
    __tablename__ = "sleep"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_sleep_external"),
        sa.Index("ix_sleep_user_started", "user_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    source: Mapped[DataSource] = mapped_column(enum_col(DataSource, "data_source"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    in_bed_min: Mapped[float | None] = mapped_column(sa.Float)
    asleep_min: Mapped[float | None] = mapped_column(sa.Float)
    performance_pct: Mapped[float | None] = mapped_column(sa.Float)
    stages_min: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    respiratory_rate: Mapped[float | None] = mapped_column(sa.Float)
    disturbances: Mapped[int | None] = mapped_column(sa.Integer)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class Recovery(Base):
    __tablename__ = "recoveries"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_recoveries_external"),
        sa.Index("ix_recoveries_user_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    source: Mapped[DataSource] = mapped_column(enum_col(DataSource, "data_source"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    score: Mapped[float | None] = mapped_column(sa.Float)
    rhr: Mapped[float | None] = mapped_column(sa.Float)
    hrv_ms: Mapped[float | None] = mapped_column(sa.Float)
    spo2: Mapped[float | None] = mapped_column(sa.Float)
    skin_temp_c: Mapped[float | None] = mapped_column(sa.Float)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


# ---------------------------------------------------------------------------------------- body


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (sa.Index("ix_measurements_user_type_at", "user_id", "type", "measured_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    type: Mapped[MeasurementType] = mapped_column(
        enum_col(MeasurementType, "measurement_type"), nullable=False
    )
    value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="manual")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    note: Mapped[str | None] = mapped_column(sa.Text)


class Lab(Base):
    __tablename__ = "labs"
    __table_args__ = (sa.Index("ix_labs_user_taken", "user_id", "taken_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    taken_at: Mapped[date] = mapped_column(sa.Date, nullable=False)
    marker: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(sa.String(32))
    ref_low: Mapped[float | None] = mapped_column(sa.Float)
    ref_high: Mapped[float | None] = mapped_column(sa.Float)
    flag: Mapped[str | None] = mapped_column(sa.String(16))
    source: Mapped[str | None] = mapped_column(sa.String(64))
    raw_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


# -------------------------------------------------------------------------------------- memory


class Note(Base):
    """Coach observations: first-class memory the agent writes and reads."""

    __tablename__ = "notes"
    __table_args__ = (sa.Index("ix_notes_user_active", "user_id", "active"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    kind: Mapped[NoteKind] = mapped_column(enum_col(NoteKind, "note_kind"), nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.8)
    source_turn_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    superseded_by: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("notes.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class UserSecret(Base):
    """An optional third-party key the user pasted into the chat, Fernet-encrypted at rest.

    One row per user and service. The plaintext leaves the chat immediately (the message is
    deleted), is never logged, and only the last four characters are kept for the "ends in …"
    line. ``privacy.delete_everything`` drops these with the rest.
    """

    __tablename__ = "user_secrets"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "service", name="uq_user_secrets_user_service"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    service: Mapped[SecretService] = mapped_column(
        enum_col(SecretService, "secret_service"), nullable=False
    )
    key_enc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    last4: Mapped[str] = mapped_column(sa.String(4), nullable=False)
    set_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (sa.Index("ix_reminders_user_status_due", "user_id", "status", "due_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    due_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="custom")
    status: Mapped[ReminderStatus] = mapped_column(
        enum_col(ReminderStatus, "reminder_status"),
        nullable=False,
        default=ReminderStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class ConversationTurn(Base):
    """Verbatim Anthropic content blocks; images are stubbed after the turn is processed."""

    __tablename__ = "conversation_turns"
    __table_args__ = (sa.Index("ix_turns_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    role: Mapped[TurnRole] = mapped_column(enum_col(TurnRole, "turn_role"), nullable=False)
    content: Mapped[list[Any]] = mapped_column(JSONType, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    telegram_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "kind", "period_start", name="uq_summaries_period"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    kind: Mapped[SummaryKind] = mapped_column(enum_col(SummaryKind, "summary_kind"), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


# -------------------------------------------------------------------------------- integrations


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "provider", name="uq_integrations_user_provider"),
        sa.Index("ix_integrations_provider_external", "provider", "external_user_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    provider: Mapped[Provider] = mapped_column(enum_col(Provider, "provider"), nullable=False)
    external_user_id: Mapped[str | None] = mapped_column(sa.String(128))
    access_token_enc: Mapped[str | None] = mapped_column(sa.Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    scopes: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[IntegrationStatus] = mapped_column(
        enum_col(IntegrationStatus, "integration_status"),
        nullable=False,
        default=IntegrationStatus.pending,
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    webhook_token: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class ProactiveSend(Base):
    __tablename__ = "proactive_sends"
    __table_args__ = (
        sa.Index("ix_proactive_user_window", "user_id", "window_key"),
        sa.Index("ix_proactive_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    trigger: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    window_key: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    step: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    sent_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    response_turn_id: Mapped[int | None] = mapped_column(sa.BigInteger)


class TokenUsage(Base):
    """Aggregated per user / date / model / purpose."""

    __tablename__ = "token_usage"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "date", "model", "purpose", name="uq_token_usage_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    model: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    purpose: Mapped[UsagePurpose] = mapped_column(
        enum_col(UsagePurpose, "usage_purpose"), nullable=False
    )
    calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    created_by: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_by: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class OAuthState(Base):
    """Single-use OAuth ``state`` values, valid for 10 minutes."""

    __tablename__ = "oauth_states"
    __table_args__ = (sa.Index("ix_oauth_states_user", "user_id"),)

    state: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, user_fk(), nullable=False)
    provider: Mapped[Provider] = mapped_column(enum_col(Provider, "provider"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


USER_OWNED_TABLES: tuple[type[Base], ...] = (
    Profile,
    Protocol,
    Day,
    Meal,
    MealItem,
    Workout,
    Sleep,
    Recovery,
    Measurement,
    Lab,
    Note,
    Reminder,
    ConversationTurn,
    Summary,
    Integration,
    ProactiveSend,
    TokenUsage,
    OAuthState,
    UserSecret,
)
"""Every model with a ``user_id`` column (used by privacy.delete_everything and tests)."""
