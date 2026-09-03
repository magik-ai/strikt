"""Repository: every read/write the rest of the code does against the database.

Rules:
- Every function that touches a user-owned table takes ``user_id`` and filters by it.
- Functions ``flush`` so ids are available; the caller owns the transaction (``commit``).
- Timestamps in are UTC-aware; timestamps out may be naive on SQLite — use ``ensure_utc``.
- Nothing here talks to the LLM or Telegram.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import Select, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from strikt.core.clock import ensure_utc
from strikt.core.types import FoodItemIn, LabMarker, Macros
from strikt.db.crypto import TokenCipher
from strikt.db.models import (
    ConversationTurn,
    DataSource,
    Day,
    DayFlag,
    Food,
    Integration,
    IntegrationStatus,
    Invite,
    ItemSource,
    Lab,
    Meal,
    MealItem,
    MealSlot,
    MealSource,
    Measurement,
    MeasurementType,
    Note,
    NoteKind,
    OAuthState,
    ProactiveSend,
    Profile,
    Protocol,
    Provider,
    Recovery,
    Reminder,
    ReminderStatus,
    Sleep,
    Summary,
    SummaryKind,
    TokenUsage,
    TurnRole,
    UsagePurpose,
    User,
    UserStatus,
    Workout,
)

# ------------------------------------------------------------------------------------ helpers


def _rowcount(result: Any) -> int:
    return int(cast("CursorResult[Any]", result).rowcount)


async def _first[T](session: AsyncSession, stmt: Select[tuple[T]]) -> T | None:
    """Typed replacement for ``session.scalar`` (whose async signature returns ``Any``)."""
    return (await session.scalars(stmt)).first()


def _dialect_name(session: AsyncSession) -> str:
    bind = session.get_bind()
    return str(bind.dialect.name)


def _text_match(session: AsyncSession, column: Any, query: str) -> Any:
    """Full-text predicate: ``to_tsvector`` on Postgres, case-insensitive LIKE elsewhere."""
    if _dialect_name(session) == "postgresql":
        return func.to_tsvector("simple", column).op("@@")(func.plainto_tsquery("simple", query))
    needle = f"%{query.lower()}%"
    return func.lower(column).like(needle)


def _like_escape(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


# -------------------------------------------------------------------------------------- users


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await _first(session, select(User).where(User.telegram_id == telegram_id))


async def get_or_create_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    chat_id: int,
    now: datetime,
    language: str | None = None,
    timezone: str | None = None,
    status: UserStatus = UserStatus.onboarding,
    invite_code: str | None = None,
) -> tuple[User, bool]:
    """Return ``(user, created)``. Updates ``chat_id`` / ``last_seen_at`` on existing users."""
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is not None:
        user.chat_id = chat_id
        user.last_seen_at = now
        await session.flush()
        return user, False
    user = User(
        telegram_id=telegram_id,
        chat_id=chat_id,
        status=status,
        language=language or "en",
        timezone=timezone or "UTC",
        created_at=now,
        last_seen_at=now,
        invite_code=invite_code,
    )
    session.add(user)
    await session.flush()
    return user, True


async def set_user_status(session: AsyncSession, user_id: int, status: UserStatus) -> None:
    await session.execute(update(User).where(User.id == user_id).values(status=status))


async def set_user_locale(
    session: AsyncSession, user_id: int, *, language: str | None = None, timezone: str | None = None
) -> None:
    values: dict[str, Any] = {}
    if language:
        values["language"] = language
    if timezone:
        values["timezone"] = timezone
    if values:
        await session.execute(update(User).where(User.id == user_id).values(**values))


async def touch_last_seen(session: AsyncSession, user_id: int, now: datetime) -> None:
    await session.execute(update(User).where(User.id == user_id).values(last_seen_at=now))


async def list_users(
    session: AsyncSession, *, statuses: Iterable[UserStatus] | None = None
) -> list[User]:
    stmt = select(User).order_by(User.id)
    if statuses is not None:
        stmt = stmt.where(User.status.in_(list(statuses)))
    return list((await session.scalars(stmt)).all())


# --------------------------------------------------------------------------- user's LLM key
# Bring-your-own-key: the Anthropic API key a user pasted into the chat. Only the Fernet
# ciphertext and the last four characters are stored; the plaintext is never logged.


async def set_llm_key(
    session: AsyncSession, user_id: int, key: str, cipher: TokenCipher, *, now: datetime
) -> str:
    """Encrypt and store ``key`` for the user (a new key replaces the old one); returns last4."""
    key = key.strip()
    if not key:
        raise ValueError("empty API key")
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} does not exist")
    # Set on the ORM object (not a bulk UPDATE) so a ``User`` already loaded in this session —
    # the handler's — sees the new key at once.
    user.llm_key_enc = cipher.encrypt(key)
    user.llm_key_last4 = key[-4:]
    user.llm_key_set_at = now
    await session.flush()
    return user.llm_key_last4


async def clear_llm_key(session: AsyncSession, user_id: int) -> bool:
    """Forget the user's key (the row stays). True when a key was stored."""
    user = await session.get(User, user_id)
    if user is None or user.llm_key_enc is None:
        return False
    user.llm_key_enc = None
    user.llm_key_last4 = None
    user.llm_key_set_at = None
    await session.flush()
    return True


async def get_llm_key(session: AsyncSession, user_id: int, cipher: TokenCipher) -> str | None:
    """The user's plaintext key, or None when none is stored. Raises ``ValueError`` when the
    ciphertext cannot be decrypted (``TOKEN_ENCRYPTION_KEY`` changed)."""
    user = await session.get(User, user_id)
    if user is None or not user.llm_key_enc:
        return None
    return cipher.decrypt(user.llm_key_enc)


# ----------------------------------------------------------------------------------- profiles

PROFILE_FIELDS: frozenset[str] = frozenset(
    column.key
    for column in Profile.__table__.columns
    if column.key not in {"user_id", "updated_at"}
)


async def get_profile(session: AsyncSession, user_id: int) -> Profile | None:
    return await session.get(Profile, user_id)


async def upsert_profile(
    session: AsyncSession, user_id: int, changes: Mapping[str, Any], *, now: datetime
) -> Profile:
    """Create or update the profile. Unknown keys raise ``ValueError`` (never silently drop)."""
    unknown = set(changes) - PROFILE_FIELDS
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    profile = await get_profile(session, user_id)
    if profile is None:
        profile = Profile(user_id=user_id, updated_at=now)
        session.add(profile)
    for key, value in changes.items():
        setattr(profile, key, value)
    profile.updated_at = now
    await session.flush()
    return profile


# ---------------------------------------------------------------------------------- protocols


async def get_active_protocol(session: AsyncSession, user_id: int) -> Protocol | None:
    return await _first(
        session, select(Protocol).where(Protocol.user_id == user_id, Protocol.active.is_(True))
    )


async def list_protocols(session: AsyncSession, user_id: int) -> list[Protocol]:
    stmt = select(Protocol).where(Protocol.user_id == user_id).order_by(Protocol.version)
    return list((await session.scalars(stmt)).all())


async def set_active_protocol(
    session: AsyncSession,
    user_id: int,
    *,
    kcal: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    fiber_g: float,
    rationale: str | None,
    now: datetime,
) -> Protocol:
    """Deactivate the current protocol and insert the next version as active."""
    await session.execute(
        update(Protocol)
        .where(Protocol.user_id == user_id, Protocol.active.is_(True))
        .values(active=False)
    )
    last_version = await session.scalar(
        select(func.max(Protocol.version)).where(Protocol.user_id == user_id)
    )
    protocol = Protocol(
        user_id=user_id,
        version=int(last_version or 0) + 1,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        fiber_g=fiber_g,
        rationale=rationale,
        active=True,
        created_at=now,
    )
    session.add(protocol)
    await session.flush()
    return protocol


def protocol_targets(protocol: Protocol | None) -> Macros:
    if protocol is None:
        return Macros.zero()
    return Macros(
        kcal=protocol.kcal,
        protein_g=protocol.protein_g,
        carbs_g=protocol.carbs_g,
        fat_g=protocol.fat_g,
        fiber_g=protocol.fiber_g,
    )


# --------------------------------------------------------------------------------------- days


async def get_day(session: AsyncSession, user_id: int, day: date) -> Day | None:
    return await _first(session, select(Day).where(Day.user_id == user_id, Day.date == day))


async def get_or_open_day(session: AsyncSession, user_id: int, day: date, *, now: datetime) -> Day:
    row = await get_day(session, user_id, day)
    if row is None:
        row = Day(user_id=user_id, date=day, opened_at=now, flags=[])
        session.add(row)
        await session.flush()
    return row


async def close_day(
    session: AsyncSession, user_id: int, day: date, *, verdict: str | None, now: datetime
) -> Day:
    row = await get_or_open_day(session, user_id, day, now=now)
    row.closed_at = now
    row.verdict = verdict
    await session.flush()
    return row


async def reopen_day(session: AsyncSession, user_id: int, day: date) -> Day | None:
    row = await get_day(session, user_id, day)
    if row is not None:
        row.closed_at = None
        await session.flush()
    return row


async def set_day_flag(
    session: AsyncSession, user_id: int, day: date, flag: DayFlag | str, on: bool, *, now: datetime
) -> Day:
    row = await get_or_open_day(session, user_id, day, now=now)
    flags = [str(f) for f in (row.flags or [])]
    value = str(DayFlag(flag))
    if on and value not in flags:
        flags.append(value)
    if not on and value in flags:
        flags.remove(value)
    row.flags = flags
    await session.flush()
    return row


async def set_day_plan(
    session: AsyncSession, user_id: int, day: date, plan: Mapping[str, Any] | None, *, now: datetime
) -> Day:
    row = await get_or_open_day(session, user_id, day, now=now)
    row.plan = dict(plan) if plan is not None else None
    await session.flush()
    return row


async def set_card_message(
    session: AsyncSession, user_id: int, day: date, message_id: int | None, *, now: datetime
) -> Day:
    row = await get_or_open_day(session, user_id, day, now=now)
    row.card_message_id = message_id
    await session.flush()
    return row


async def list_days_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[Day]:
    stmt = (
        select(Day)
        .where(Day.user_id == user_id, Day.date >= date_from, Day.date <= date_to)
        .order_by(Day.date)
    )
    return list((await session.scalars(stmt)).all())


# -------------------------------------------------------------------------------------- meals

MEAL_ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "brand",
        "restaurant",
        "quantity",
        "unit",
        "grams",
        "kcal",
        "protein_g",
        "carbs_g",
        "fat_g",
        "fiber_g",
        "sodium_mg",
        "alcohol_g",
        "confidence",
        "source",
        "source_url",
        "countable",
        "flags",
    }
)


def _item_from_input(user_id: int, item: FoodItemIn, position: int) -> MealItem:
    return MealItem(
        user_id=user_id,
        name=item.name,
        brand=item.brand,
        restaurant=item.restaurant,
        quantity=item.quantity,
        unit=item.unit,
        grams=item.grams,
        kcal=item.macros.kcal,
        protein_g=item.macros.protein_g,
        carbs_g=item.macros.carbs_g,
        fat_g=item.macros.fat_g,
        fiber_g=item.macros.fiber_g,
        sodium_mg=item.macros.sodium_mg,
        alcohol_g=item.macros.alcohol_g,
        confidence=item.confidence,
        source=ItemSource(item.source),
        source_url=item.source_url,
        countable=item.countable,
        model_estimate=item.macros.model_dump(),
        flags=[],
        position=position,
    )


def item_macros(item: MealItem) -> Macros:
    return Macros(
        kcal=item.kcal,
        protein_g=item.protein_g,
        carbs_g=item.carbs_g,
        fat_g=item.fat_g,
        fiber_g=item.fiber_g,
        sodium_mg=item.sodium_mg,
        alcohol_g=item.alcohol_g,
    )


def meal_macros(meal: Meal) -> Macros:
    total = Macros.zero()
    for item in meal.items:
        total = total + item_macros(item)
    return total


async def add_meal_with_items(
    session: AsyncSession,
    user_id: int,
    *,
    day_date: date,
    items: Sequence[FoodItemIn],
    slot: MealSlot | str = MealSlot.unknown,
    source: MealSource | str = MealSource.text,
    logged_at: datetime,
    eaten_at: datetime | None = None,
    raw_ref: Mapping[str, Any] | None = None,
    note: str | None = None,
    item_flags: Mapping[int, Sequence[str]] | None = None,
) -> Meal:
    """Insert a meal and its items in order. ``item_flags`` maps item position → sanity codes."""
    meal = Meal(
        user_id=user_id,
        day_date=day_date,
        slot=MealSlot(slot),
        source=MealSource(source),
        logged_at=logged_at,
        eaten_at=eaten_at,
        raw_ref=dict(raw_ref) if raw_ref else None,
        note=note,
    )
    for position, item in enumerate(items):
        row = _item_from_input(user_id, item, position)
        if item_flags and position in item_flags:
            row.flags = list(item_flags[position])
        meal.items.append(row)
    session.add(meal)
    await session.flush()
    return meal


async def get_meal(
    session: AsyncSession, user_id: int, meal_id: int, *, include_deleted: bool = False
) -> Meal | None:
    stmt = (
        select(Meal)
        .where(Meal.user_id == user_id, Meal.id == meal_id)
        .options(selectinload(Meal.items))
    )
    if not include_deleted:
        stmt = stmt.where(Meal.deleted_at.is_(None))
    return await _first(session, stmt)


async def get_meal_item(session: AsyncSession, user_id: int, item_id: int) -> MealItem | None:
    return await _first(
        session, select(MealItem).where(MealItem.user_id == user_id, MealItem.id == item_id)
    )


async def update_meal_item(
    session: AsyncSession,
    user_id: int,
    item_id: int,
    changes: Mapping[str, Any],
    *,
    user_correction: Mapping[str, Any] | None = None,
) -> MealItem | None:
    """Apply ``changes`` (subset of ``MEAL_ITEM_FIELDS``); keep the user's correction on the row."""
    unknown = set(changes) - MEAL_ITEM_FIELDS
    if unknown:
        raise ValueError(f"unknown meal item fields: {sorted(unknown)}")
    item = await get_meal_item(session, user_id, item_id)
    if item is None:
        return None
    for key, value in changes.items():
        setattr(item, key, ItemSource(value) if key == "source" else value)
    if user_correction is not None:
        merged = dict(item.user_correction or {})
        merged.update(user_correction)
        item.user_correction = merged
    await session.flush()
    return item


async def update_meal(
    session: AsyncSession,
    user_id: int,
    meal_id: int,
    *,
    slot: MealSlot | str | None = None,
    eaten_at: datetime | None = None,
    note: str | None = None,
    day_date: date | None = None,
) -> Meal | None:
    meal = await get_meal(session, user_id, meal_id)
    if meal is None:
        return None
    if slot is not None:
        meal.slot = MealSlot(slot)
    if eaten_at is not None:
        meal.eaten_at = eaten_at
    if note is not None:
        meal.note = note
    if day_date is not None:
        meal.day_date = day_date
    await session.flush()
    return meal


async def soft_delete_meal(
    session: AsyncSession, user_id: int, meal_id: int, *, now: datetime
) -> bool:
    result = await session.execute(
        update(Meal)
        .where(Meal.user_id == user_id, Meal.id == meal_id, Meal.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    return _rowcount(result) > 0


async def restore_meal(session: AsyncSession, user_id: int, meal_id: int) -> bool:
    result = await session.execute(
        update(Meal)
        .where(Meal.user_id == user_id, Meal.id == meal_id, Meal.deleted_at.is_not(None))
        .values(deleted_at=None)
    )
    return _rowcount(result) > 0


async def last_meal(
    session: AsyncSession, user_id: int, *, include_deleted: bool = False
) -> Meal | None:
    stmt = (
        select(Meal)
        .where(Meal.user_id == user_id)
        .options(selectinload(Meal.items))
        .order_by(Meal.logged_at.desc(), Meal.id.desc())
        .limit(1)
    )
    if not include_deleted:
        stmt = stmt.where(Meal.deleted_at.is_(None))
    return await _first(session, stmt)


async def list_meals_for_date(
    session: AsyncSession, user_id: int, day: date, *, include_deleted: bool = False
) -> list[Meal]:
    stmt = (
        select(Meal)
        .where(Meal.user_id == user_id, Meal.day_date == day)
        .options(selectinload(Meal.items))
        .order_by(Meal.logged_at, Meal.id)
    )
    if not include_deleted:
        stmt = stmt.where(Meal.deleted_at.is_(None))
    return list((await session.scalars(stmt)).all())


async def list_meals_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[Meal]:
    stmt = (
        select(Meal)
        .where(
            Meal.user_id == user_id,
            Meal.day_date >= date_from,
            Meal.day_date <= date_to,
            Meal.deleted_at.is_(None),
        )
        .options(selectinload(Meal.items))
        .order_by(Meal.day_date, Meal.logged_at, Meal.id)
    )
    return list((await session.scalars(stmt)).all())


async def search_meal_items(
    session: AsyncSession, user_id: int, query: str, *, limit: int = 20
) -> list[MealItem]:
    stmt = (
        select(MealItem)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(
            MealItem.user_id == user_id,
            Meal.deleted_at.is_(None),
            _text_match(session, MealItem.name, _like_escape(query)),
        )
        .order_by(MealItem.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# -------------------------------------------------------------------------------------- foods

_KEY_STRIP = re.compile(r"[^\w\s]", re.UNICODE)


def make_food_key(name: str, brand: str | None = None, restaurant: str | None = None) -> str:
    def norm(value: str | None) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", _KEY_STRIP.sub("", value.casefold())).strip()

    return "|".join((norm(name), norm(brand), norm(restaurant)))


async def get_food_by_key(session: AsyncSession, key: str) -> Food | None:
    return await _first(session, select(Food).where(Food.key == key))


async def get_food_by_barcode(session: AsyncSession, barcode: str) -> Food | None:
    return await _first(
        session,
        select(Food).where(Food.barcode == barcode).order_by(Food.fetched_at.desc()).limit(1),
    )


async def upsert_food(
    session: AsyncSession,
    *,
    name: str,
    per_100g: Macros,
    source: ItemSource | str,
    fetched_at: datetime,
    brand: str | None = None,
    restaurant: str | None = None,
    barcode: str | None = None,
    serving_g: float | None = None,
    serving_desc: str | None = None,
    source_url: str | None = None,
    confidence: float = 0.8,
) -> Food:
    key = make_food_key(name, brand, restaurant)
    food = await get_food_by_key(session, key)
    if food is None:
        food = Food(key=key, name=name, per_100g=per_100g.model_dump(), source=ItemSource(source))
        session.add(food)
    food.name = name
    food.brand = brand
    food.restaurant = restaurant
    food.barcode = barcode
    food.per_100g = per_100g.model_dump()
    food.serving_g = serving_g
    food.serving_desc = serving_desc
    food.source = ItemSource(source)
    food.source_url = source_url
    food.confidence = confidence
    food.fetched_at = fetched_at
    await session.flush()
    return food


# ----------------------------------------------------------------------------------- workouts


@dataclass(frozen=True)
class WorkoutAverages:
    count: int
    duration_min: float | None
    strain: float | None
    kcal: float | None
    avg_hr: float | None


async def get_workout(session: AsyncSession, user_id: int, workout_id: int) -> Workout | None:
    return await _first(
        session, select(Workout).where(Workout.user_id == user_id, Workout.id == workout_id)
    )


async def upsert_workout_by_external(
    session: AsyncSession,
    user_id: int,
    *,
    source: DataSource | str,
    external_id: str | None,
    sport: str,
    started_at: datetime,
    now: datetime,
    ended_at: datetime | None = None,
    duration_min: float | None = None,
    strain: float | None = None,
    kcal: float | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
    zones_min: Mapping[str, Any] | None = None,
    distance_m: float | None = None,
    raw: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> tuple[Workout, bool]:
    """Insert or update by ``(user_id, source, external_id)``. Returns ``(row, created)``."""
    src = DataSource(source)
    row: Workout | None = None
    if external_id is not None:
        row = await session.scalar(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.source == src,
                Workout.external_id == external_id,
            )
        )
    created = row is None
    if row is None:
        row = Workout(user_id=user_id, source=src, external_id=external_id, created_at=now)
        session.add(row)
    row.sport = sport
    row.started_at = started_at
    row.ended_at = ended_at
    row.duration_min = duration_min
    row.strain = strain
    row.kcal = kcal
    row.avg_hr = avg_hr
    row.max_hr = max_hr
    row.zones_min = dict(zones_min) if zones_min is not None else None
    row.distance_m = distance_m
    row.raw = dict(raw) if raw is not None else None
    if note is not None:
        row.note = note
    await session.flush()
    return row, created


async def list_workouts_range(
    session: AsyncSession, user_id: int, start: datetime, end: datetime
) -> list[Workout]:
    stmt = (
        select(Workout)
        .where(Workout.user_id == user_id, Workout.started_at >= start, Workout.started_at < end)
        .order_by(Workout.started_at)
    )
    return list((await session.scalars(stmt)).all())


async def last_same_sport(
    session: AsyncSession,
    user_id: int,
    sport: str,
    *,
    before: datetime,
    exclude_id: int | None = None,
) -> Workout | None:
    stmt = (
        select(Workout)
        .where(
            Workout.user_id == user_id,
            func.lower(Workout.sport) == sport.lower(),
            Workout.started_at < before,
        )
        .order_by(Workout.started_at.desc())
        .limit(1)
    )
    if exclude_id is not None:
        stmt = stmt.where(Workout.id != exclude_id)
    return await _first(session, stmt)


async def avg_30d(
    session: AsyncSession, user_id: int, *, now: datetime, sport: str | None = None, days: int = 30
) -> WorkoutAverages:
    since = now - timedelta(days=days)
    stmt = select(
        func.count(Workout.id),
        func.avg(Workout.duration_min),
        func.avg(Workout.strain),
        func.avg(Workout.kcal),
        func.avg(Workout.avg_hr),
    ).where(Workout.user_id == user_id, Workout.started_at >= since)
    if sport is not None:
        stmt = stmt.where(func.lower(Workout.sport) == sport.lower())
    row = (await session.execute(stmt)).one()
    count = int(row[0] or 0)

    def opt(value: Any) -> float | None:
        return None if value is None else float(value)

    return WorkoutAverages(count, opt(row[1]), opt(row[2]), opt(row[3]), opt(row[4]))


# -------------------------------------------------------------------------------------- sleep


async def upsert_sleep_by_external(
    session: AsyncSession,
    user_id: int,
    *,
    source: DataSource | str,
    external_id: str | None,
    started_at: datetime,
    ended_at: datetime,
    now: datetime,
    in_bed_min: float | None = None,
    asleep_min: float | None = None,
    performance_pct: float | None = None,
    stages_min: Mapping[str, Any] | None = None,
    respiratory_rate: float | None = None,
    disturbances: int | None = None,
    raw: Mapping[str, Any] | None = None,
) -> tuple[Sleep, bool]:
    src = DataSource(source)
    row: Sleep | None = None
    if external_id is not None:
        row = await session.scalar(
            select(Sleep).where(
                Sleep.user_id == user_id, Sleep.source == src, Sleep.external_id == external_id
            )
        )
    created = row is None
    if row is None:
        row = Sleep(user_id=user_id, source=src, external_id=external_id, created_at=now)
        session.add(row)
    row.started_at = started_at
    row.ended_at = ended_at
    row.in_bed_min = in_bed_min
    row.asleep_min = asleep_min
    row.performance_pct = performance_pct
    row.stages_min = dict(stages_min) if stages_min is not None else None
    row.respiratory_rate = respiratory_rate
    row.disturbances = disturbances
    row.raw = dict(raw) if raw is not None else None
    await session.flush()
    return row, created


async def list_sleep_range(
    session: AsyncSession, user_id: int, start: datetime, end: datetime
) -> list[Sleep]:
    stmt = (
        select(Sleep)
        .where(Sleep.user_id == user_id, Sleep.ended_at >= start, Sleep.ended_at < end)
        .order_by(Sleep.started_at)
    )
    return list((await session.scalars(stmt)).all())


async def last_sleep(session: AsyncSession, user_id: int) -> Sleep | None:
    return await _first(
        session,
        select(Sleep).where(Sleep.user_id == user_id).order_by(Sleep.ended_at.desc()).limit(1),
    )


# --------------------------------------------------------------------------------- recoveries


async def upsert_recovery_by_external(
    session: AsyncSession,
    user_id: int,
    *,
    source: DataSource | str,
    external_id: str | None,
    day: date,
    score: float | None = None,
    rhr: float | None = None,
    hrv_ms: float | None = None,
    spo2: float | None = None,
    skin_temp_c: float | None = None,
    raw: Mapping[str, Any] | None = None,
) -> tuple[Recovery, bool]:
    src = DataSource(source)
    row: Recovery | None = None
    if external_id is not None:
        row = await session.scalar(
            select(Recovery).where(
                Recovery.user_id == user_id,
                Recovery.source == src,
                Recovery.external_id == external_id,
            )
        )
    created = row is None
    if row is None:
        row = Recovery(user_id=user_id, source=src, external_id=external_id, date=day)
        session.add(row)
    row.date = day
    row.score = score
    row.rhr = rhr
    row.hrv_ms = hrv_ms
    row.spo2 = spo2
    row.skin_temp_c = skin_temp_c
    row.raw = dict(raw) if raw is not None else None
    await session.flush()
    return row, created


async def list_recoveries_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[Recovery]:
    stmt = (
        select(Recovery)
        .where(Recovery.user_id == user_id, Recovery.date >= date_from, Recovery.date <= date_to)
        .order_by(Recovery.date)
    )
    return list((await session.scalars(stmt)).all())


async def recovery_for_date(session: AsyncSession, user_id: int, day: date) -> Recovery | None:
    return await _first(
        session,
        select(Recovery)
        .where(Recovery.user_id == user_id, Recovery.date == day)
        .order_by(Recovery.id.desc())
        .limit(1),
    )


# ------------------------------------------------------------------------------- measurements


async def add_measurement(
    session: AsyncSession,
    user_id: int,
    *,
    type: MeasurementType | str,
    value: float,
    unit: str,
    measured_at: datetime,
    source: str = "manual",
    raw: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> Measurement:
    row = Measurement(
        user_id=user_id,
        type=MeasurementType(type),
        value=value,
        unit=unit,
        measured_at=measured_at,
        source=source,
        raw=dict(raw) if raw is not None else None,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def latest_by_type(
    session: AsyncSession, user_id: int, type: MeasurementType | str
) -> Measurement | None:
    return await _first(
        session,
        select(Measurement)
        .where(Measurement.user_id == user_id, Measurement.type == MeasurementType(type))
        .order_by(Measurement.measured_at.desc(), Measurement.id.desc())
        .limit(1),
    )


async def list_measurements_range(
    session: AsyncSession,
    user_id: int,
    start: datetime,
    end: datetime,
    *,
    type: MeasurementType | str | None = None,
) -> list[Measurement]:
    stmt = (
        select(Measurement)
        .where(
            Measurement.user_id == user_id,
            Measurement.measured_at >= start,
            Measurement.measured_at < end,
        )
        .order_by(Measurement.measured_at)
    )
    if type is not None:
        stmt = stmt.where(Measurement.type == MeasurementType(type))
    return list((await session.scalars(stmt)).all())


async def days_since_last(
    session: AsyncSession,
    user_id: int,
    type: MeasurementType | str,
    *,
    now: datetime,
) -> int | None:
    """Whole days since the latest measurement of ``type``; None when there is none."""
    latest = await latest_by_type(session, user_id, type)
    if latest is None:
        return None
    delta = ensure_utc(now) - ensure_utc(latest.measured_at)
    return max(0, delta.days)


async def average_by_type(
    session: AsyncSession,
    user_id: int,
    type: MeasurementType | str,
    *,
    since: datetime,
) -> float | None:
    value = await session.scalar(
        select(func.avg(Measurement.value)).where(
            Measurement.user_id == user_id,
            Measurement.type == MeasurementType(type),
            Measurement.measured_at >= since,
        )
    )
    return None if value is None else float(value)


# --------------------------------------------------------------------------------------- labs


async def add_labs(
    session: AsyncSession,
    user_id: int,
    *,
    taken_at: date,
    markers: Sequence[LabMarker],
    source: str | None = None,
    raw_ref: Mapping[str, Any] | None = None,
) -> list[Lab]:
    rows = [
        Lab(
            user_id=user_id,
            taken_at=taken_at,
            marker=m.marker,
            value=m.value,
            unit=m.unit,
            ref_low=m.ref_low,
            ref_high=m.ref_high,
            flag=m.flag,
            source=source,
            raw_ref=dict(raw_ref) if raw_ref is not None else None,
        )
        for m in markers
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def list_labs(
    session: AsyncSession, user_id: int, *, marker: str | None = None, limit: int = 200
) -> list[Lab]:
    stmt = (
        select(Lab)
        .where(Lab.user_id == user_id)
        .order_by(Lab.taken_at.desc(), Lab.marker)
        .limit(limit)
    )
    if marker is not None:
        stmt = stmt.where(func.lower(Lab.marker) == marker.lower())
    return list((await session.scalars(stmt)).all())


# -------------------------------------------------------------------------------------- notes


async def get_note(session: AsyncSession, user_id: int, note_id: int) -> Note | None:
    return await _first(session, select(Note).where(Note.user_id == user_id, Note.id == note_id))


async def add_note(
    session: AsyncSession,
    user_id: int,
    *,
    kind: NoteKind | str,
    text: str,
    confidence: float,
    now: datetime,
    source_turn_id: int | None = None,
    expires_at: datetime | None = None,
) -> Note:
    note = Note(
        user_id=user_id,
        kind=NoteKind(kind),
        text=text,
        confidence=confidence,
        source_turn_id=source_turn_id,
        created_at=now,
        expires_at=expires_at,
        active=True,
    )
    session.add(note)
    await session.flush()
    return note


async def list_active_notes(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
    kinds: Iterable[NoteKind | str] | None = None,
) -> list[Note]:
    stmt = (
        select(Note)
        .where(Note.user_id == user_id, Note.active.is_(True))
        .order_by(Note.kind, Note.created_at, Note.id)
    )
    if now is not None:
        stmt = stmt.where(sa.or_(Note.expires_at.is_(None), Note.expires_at > now))
    if kinds is not None:
        stmt = stmt.where(Note.kind.in_([NoteKind(k) for k in kinds]))
    return list((await session.scalars(stmt)).all())


async def supersede_note(
    session: AsyncSession,
    user_id: int,
    old_id: int,
    *,
    text: str,
    confidence: float,
    now: datetime,
    kind: NoteKind | str | None = None,
    expires_at: datetime | None = None,
    source_turn_id: int | None = None,
) -> Note | None:
    """Retire ``old_id`` and link it to a new note. Returns the new note, or None if not found."""
    old = await get_note(session, user_id, old_id)
    if old is None:
        return None
    new = await add_note(
        session,
        user_id,
        kind=kind or old.kind,
        text=text,
        confidence=confidence,
        now=now,
        source_turn_id=source_turn_id,
        expires_at=expires_at,
    )
    old.active = False
    old.superseded_by = new.id
    await session.flush()
    return new


async def retire_note(session: AsyncSession, user_id: int, note_id: int) -> bool:
    result = await session.execute(
        update(Note)
        .where(Note.user_id == user_id, Note.id == note_id, Note.active.is_(True))
        .values(active=False)
    )
    return _rowcount(result) > 0


async def confirm_note(session: AsyncSession, user_id: int, note_id: int, *, now: datetime) -> bool:
    result = await session.execute(
        update(Note)
        .where(Note.user_id == user_id, Note.id == note_id)
        .values(last_confirmed_at=now)
    )
    return _rowcount(result) > 0


async def search_notes(
    session: AsyncSession, user_id: int, query: str, *, limit: int = 20
) -> list[Note]:
    stmt = (
        select(Note)
        .where(Note.user_id == user_id, _text_match(session, Note.text, _like_escape(query)))
        .order_by(Note.active.desc(), Note.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------------------------- reminders


async def add_reminder(
    session: AsyncSession,
    user_id: int,
    *,
    due_at: datetime,
    text: str,
    now: datetime,
    kind: str = "custom",
) -> Reminder:
    row = Reminder(
        user_id=user_id,
        due_at=due_at,
        text=text,
        kind=kind,
        status=ReminderStatus.pending,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def pending_reminders(
    session: AsyncSession, user_id: int | None = None, *, due_before: datetime | None = None
) -> list[Reminder]:
    """Pending reminders, optionally for one user and/or due before an instant (scheduler)."""
    stmt = (
        select(Reminder).where(Reminder.status == ReminderStatus.pending).order_by(Reminder.due_at)
    )
    if user_id is not None:
        stmt = stmt.where(Reminder.user_id == user_id)
    if due_before is not None:
        stmt = stmt.where(Reminder.due_at <= due_before)
    return list((await session.scalars(stmt)).all())


async def cancel_reminder(session: AsyncSession, user_id: int, reminder_id: int) -> bool:
    result = await session.execute(
        update(Reminder)
        .where(
            Reminder.user_id == user_id,
            Reminder.id == reminder_id,
            Reminder.status == ReminderStatus.pending,
        )
        .values(status=ReminderStatus.cancelled)
    )
    return _rowcount(result) > 0


async def mark_reminder_sent(session: AsyncSession, user_id: int, reminder_id: int) -> bool:
    result = await session.execute(
        update(Reminder)
        .where(
            Reminder.user_id == user_id,
            Reminder.id == reminder_id,
            Reminder.status == ReminderStatus.pending,
        )
        .values(status=ReminderStatus.sent)
    )
    return _rowcount(result) > 0


# -------------------------------------------------------------------------------------- turns


def stub_image_blocks(content: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replace image/document blocks with ``[image: <hash>]`` text stubs (pure function)."""
    import hashlib

    out: list[dict[str, Any]] = []
    for block in content:
        btype = block.get("type")
        if btype in {"image", "document"}:
            source = block.get("source") or {}
            data = str(source.get("data") or source.get("url") or source.get("file_id") or "")
            digest = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16] if data else "unknown"
            out.append({"type": "text", "text": f"[{btype}: {digest}]"})
        else:
            out.append(dict(block))
    return out


def content_to_text(content: Sequence[Mapping[str, Any]]) -> str:
    parts = [str(b.get("text", "")) for b in content if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


async def add_turn(
    session: AsyncSession,
    user_id: int,
    *,
    role: TurnRole | str,
    content: Sequence[Mapping[str, Any]],
    now: datetime,
    text: str | None = None,
    telegram_message_id: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> ConversationTurn:
    turn = ConversationTurn(
        user_id=user_id,
        role=TurnRole(role),
        content=[dict(b) for b in content],
        text=text if text is not None else content_to_text(content),
        telegram_message_id=telegram_message_id,
        created_at=now,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    session.add(turn)
    await session.flush()
    return turn


async def count_turns(session: AsyncSession, user_id: int) -> int:
    value = await session.scalar(
        select(func.count(ConversationTurn.id)).where(ConversationTurn.user_id == user_id)
    )
    return int(value or 0)


async def last_n_turns(session: AsyncSession, user_id: int, n: int) -> list[ConversationTurn]:
    """The last ``n`` turns in chronological order."""
    stmt = (
        select(ConversationTurn)
        .where(ConversationTurn.user_id == user_id)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
        .limit(n)
    )
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    return rows


async def mark_stub_images(
    session: AsyncSession, user_id: int, turn_id: int
) -> ConversationTurn | None:
    turn = await session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.user_id == user_id, ConversationTurn.id == turn_id
        )
    )
    if turn is None:
        return None
    turn.content = stub_image_blocks(turn.content)
    await session.flush()
    return turn


async def search_turns(
    session: AsyncSession, user_id: int, query: str, *, limit: int = 20
) -> list[ConversationTurn]:
    """FTS over turn text: ``to_tsvector`` on Postgres, LIKE fallback on SQLite."""
    stmt = (
        select(ConversationTurn)
        .where(
            ConversationTurn.user_id == user_id,
            _text_match(session, ConversationTurn.text, _like_escape(query)),
        )
        .order_by(ConversationTurn.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------------------------- summaries


async def get_summary(
    session: AsyncSession, user_id: int, kind: SummaryKind | str, period_start: date
) -> Summary | None:
    return await _first(
        session,
        select(Summary).where(
            Summary.user_id == user_id,
            Summary.kind == SummaryKind(kind),
            Summary.period_start == period_start,
        ),
    )


async def upsert_summary(
    session: AsyncSession,
    user_id: int,
    *,
    kind: SummaryKind | str,
    period_start: date,
    period_end: date,
    text: str,
    data: Mapping[str, Any] | None,
    now: datetime,
) -> Summary:
    row = await get_summary(session, user_id, kind, period_start)
    if row is None:
        row = Summary(
            user_id=user_id,
            kind=SummaryKind(kind),
            period_start=period_start,
            period_end=period_end,
            text=text,
            data=dict(data) if data is not None else None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.period_end = period_end
        row.text = text
        row.data = dict(data) if data is not None else None
        row.updated_at = now
    await session.flush()
    return row


async def list_recent_summaries(
    session: AsyncSession, user_id: int, kind: SummaryKind | str, *, limit: int = 7
) -> list[Summary]:
    """Most recent first."""
    stmt = (
        select(Summary)
        .where(Summary.user_id == user_id, Summary.kind == SummaryKind(kind))
        .order_by(Summary.period_start.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def search_summaries(
    session: AsyncSession, user_id: int, query: str, *, limit: int = 10
) -> list[Summary]:
    stmt = (
        select(Summary)
        .where(Summary.user_id == user_id, _text_match(session, Summary.text, _like_escape(query)))
        .order_by(Summary.period_start.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# ------------------------------------------------------------------------------- integrations


@dataclass(frozen=True)
class IntegrationTokens:
    access_token: str | None
    refresh_token: str | None
    expires_at: datetime | None


def generate_webhook_token() -> str:
    return secrets.token_urlsafe(32)


async def get_integration(
    session: AsyncSession, user_id: int, provider: Provider | str
) -> Integration | None:
    return await _first(
        session,
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == Provider(provider)
        ),
    )


async def list_integrations(
    session: AsyncSession, *, provider: Provider | str | None = None
) -> list[Integration]:
    stmt = select(Integration).order_by(Integration.id)
    if provider is not None:
        stmt = stmt.where(Integration.provider == Provider(provider))
    return list((await session.scalars(stmt)).all())


async def upsert_integration(
    session: AsyncSession,
    user_id: int,
    provider: Provider | str,
    *,
    now: datetime,
    external_user_id: str | None = None,
    scopes: str | None = None,
    status: IntegrationStatus | str | None = None,
    webhook_token: str | None = None,
) -> Integration:
    row = await get_integration(session, user_id, provider)
    if row is None:
        row = Integration(
            user_id=user_id,
            provider=Provider(provider),
            status=IntegrationStatus.pending,
            created_at=now,
        )
        session.add(row)
    if external_user_id is not None:
        row.external_user_id = external_user_id
    if scopes is not None:
        row.scopes = scopes
    if status is not None:
        row.status = IntegrationStatus(status)
    if webhook_token is not None:
        row.webhook_token = webhook_token
    await session.flush()
    return row


async def set_integration_tokens(
    session: AsyncSession,
    cipher: TokenCipher,
    user_id: int,
    provider: Provider | str,
    *,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: datetime | None,
    now: datetime,
    scopes: str | None = None,
    external_user_id: str | None = None,
) -> Integration:
    """Store tokens encrypted with Fernet and mark the integration connected."""
    row = await upsert_integration(
        session, user_id, provider, now=now, scopes=scopes, external_user_id=external_user_id
    )
    row.access_token_enc = cipher.encrypt_optional(access_token)
    row.refresh_token_enc = cipher.encrypt_optional(refresh_token)
    row.expires_at = expires_at
    row.status = IntegrationStatus.connected
    await session.flush()
    return row


def integration_tokens(cipher: TokenCipher, row: Integration) -> IntegrationTokens:
    return IntegrationTokens(
        access_token=cipher.decrypt_optional(row.access_token_enc),
        refresh_token=cipher.decrypt_optional(row.refresh_token_enc),
        expires_at=row.expires_at,
    )


async def set_integration_status(
    session: AsyncSession,
    user_id: int,
    provider: Provider | str,
    status: IntegrationStatus | str,
    *,
    last_sync_at: datetime | None = None,
) -> bool:
    values: dict[str, Any] = {"status": IntegrationStatus(status)}
    if last_sync_at is not None:
        values["last_sync_at"] = last_sync_at
    result = await session.execute(
        update(Integration)
        .where(Integration.user_id == user_id, Integration.provider == Provider(provider))
        .values(**values)
    )
    return _rowcount(result) > 0


async def integration_by_webhook_token(session: AsyncSession, token: str) -> Integration | None:
    return await _first(session, select(Integration).where(Integration.webhook_token == token))


async def integration_by_external_user(
    session: AsyncSession, provider: Provider | str, external_user_id: str
) -> Integration | None:
    return await _first(
        session,
        select(Integration).where(
            Integration.provider == Provider(provider),
            Integration.external_user_id == external_user_id,
        ),
    )


async def clear_integration(session: AsyncSession, user_id: int, provider: Provider | str) -> bool:
    """Revoke = delete: tokens are removed, not just disconnected (research/01 lesson 10)."""
    result = await session.execute(
        sa.delete(Integration).where(
            Integration.user_id == user_id, Integration.provider == Provider(provider)
        )
    )
    return _rowcount(result) > 0


# ---------------------------------------------------------------------------- proactive sends


async def add_proactive_send(
    session: AsyncSession,
    user_id: int,
    *,
    trigger: str,
    window_key: str,
    step: int,
    sent_at: datetime,
    text: str,
    telegram_message_id: int | None = None,
) -> ProactiveSend:
    row = ProactiveSend(
        user_id=user_id,
        trigger=trigger,
        window_key=window_key,
        step=step,
        sent_at=sent_at,
        text=text,
        telegram_message_id=telegram_message_id,
    )
    session.add(row)
    await session.flush()
    return row


async def last_send_for_window(
    session: AsyncSession, user_id: int, window_key: str
) -> ProactiveSend | None:
    return await _first(
        session,
        select(ProactiveSend)
        .where(ProactiveSend.user_id == user_id, ProactiveSend.window_key == window_key)
        .order_by(ProactiveSend.sent_at.desc(), ProactiveSend.id.desc())
        .limit(1),
    )


async def count_sends_today(
    session: AsyncSession,
    user_id: int,
    *,
    since: datetime,
    exclude_triggers: Sequence[str] = (),
) -> int:
    """Sends since ``since`` (the caller passes the local day start expressed in UTC), minus the
    triggers in ``exclude_triggers`` (user-requested reminders do not count against the cap)."""
    stmt = select(func.count(ProactiveSend.id)).where(
        ProactiveSend.user_id == user_id, ProactiveSend.sent_at >= since
    )
    if exclude_triggers:
        stmt = stmt.where(ProactiveSend.trigger.not_in(list(exclude_triggers)))
    value = await session.scalar(stmt)
    return int(value or 0)


async def list_sends_since(
    session: AsyncSession, user_id: int, *, since: datetime, trigger: str | None = None
) -> list[ProactiveSend]:
    stmt = (
        select(ProactiveSend)
        .where(ProactiveSend.user_id == user_id, ProactiveSend.sent_at >= since)
        .order_by(ProactiveSend.sent_at)
    )
    if trigger is not None:
        stmt = stmt.where(ProactiveSend.trigger == trigger)
    return list((await session.scalars(stmt)).all())


async def mark_responded(
    session: AsyncSession, user_id: int, *, at: datetime, turn_id: int | None = None
) -> int:
    """Mark every unanswered send as responded (a reply resets all open ladders)."""
    result = await session.execute(
        update(ProactiveSend)
        .where(ProactiveSend.user_id == user_id, ProactiveSend.responded_at.is_(None))
        .values(responded_at=at, response_turn_id=turn_id)
    )
    return _rowcount(result)


async def response_rate(
    session: AsyncSession,
    user_id: int,
    *,
    trigger: str | None = None,
    since: datetime | None = None,
) -> float | None:
    """Share of sends that got a reply; None when there were no sends."""
    conditions: list[Any] = [ProactiveSend.user_id == user_id]
    if trigger is not None:
        conditions.append(ProactiveSend.trigger == trigger)
    if since is not None:
        conditions.append(ProactiveSend.sent_at >= since)
    row = (
        await session.execute(
            select(
                func.count(ProactiveSend.id),
                func.count(ProactiveSend.responded_at),
            ).where(*conditions)
        )
    ).one()
    total = int(row[0] or 0)
    if total == 0:
        return None
    return int(row[1] or 0) / total


# -------------------------------------------------------------------------------- token usage


@dataclass(frozen=True)
class UsageTotals:
    calls: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    cost_usd: float


async def add_usage(
    session: AsyncSession,
    user_id: int,
    *,
    day: date,
    model: str,
    purpose: UsagePurpose | str,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    cost_usd: float,
    calls: int = 1,
) -> TokenUsage:
    """Aggregate into the (user, date, model, purpose) row."""
    purpose_enum = UsagePurpose(purpose)
    row = await session.scalar(
        select(TokenUsage).where(
            TokenUsage.user_id == user_id,
            TokenUsage.date == day,
            TokenUsage.model == model,
            TokenUsage.purpose == purpose_enum,
        )
    )
    if row is None:
        row = TokenUsage(user_id=user_id, date=day, model=model, purpose=purpose_enum)
        session.add(row)
    row.calls = (row.calls or 0) + calls
    row.input_tokens = (row.input_tokens or 0) + input_tokens
    row.cache_read_tokens = (row.cache_read_tokens or 0) + cache_read_tokens
    row.cache_write_tokens = (row.cache_write_tokens or 0) + cache_write_tokens
    row.output_tokens = (row.output_tokens or 0) + output_tokens
    row.cost_usd = (row.cost_usd or 0.0) + cost_usd
    await session.flush()
    return row


async def usage_for_date(session: AsyncSession, user_id: int, day: date) -> list[TokenUsage]:
    stmt = (
        select(TokenUsage)
        .where(TokenUsage.user_id == user_id, TokenUsage.date == day)
        .order_by(TokenUsage.purpose)
    )
    return list((await session.scalars(stmt)).all())


async def usage_totals(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> UsageTotals:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(TokenUsage.calls), 0),
                func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                func.coalesce(func.sum(TokenUsage.cache_read_tokens), 0),
                func.coalesce(func.sum(TokenUsage.cache_write_tokens), 0),
                func.coalesce(func.sum(TokenUsage.output_tokens), 0),
                func.coalesce(func.sum(TokenUsage.cost_usd), 0.0),
            ).where(
                TokenUsage.user_id == user_id,
                TokenUsage.date >= date_from,
                TokenUsage.date <= date_to,
            )
        )
    ).one()
    return UsageTotals(
        int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]), float(row[5])
    )


# ------------------------------------------------------------------------------------ invites


def generate_invite_code() -> str:
    return secrets.token_urlsafe(9)


async def create_invite(
    session: AsyncSession, *, now: datetime, created_by: int | None = None, code: str | None = None
) -> Invite:
    invite = Invite(code=code or generate_invite_code(), created_by=created_by, created_at=now)
    session.add(invite)
    await session.flush()
    return invite


async def get_invite(session: AsyncSession, code: str) -> Invite | None:
    return await session.get(Invite, code)


async def consume_invite(
    session: AsyncSession, code: str, *, used_by: int, now: datetime
) -> Invite | None:
    """Mark the invite used. Returns None when the code is unknown or already used."""
    invite = await get_invite(session, code)
    if invite is None or invite.used_at is not None:
        return None
    invite.used_by = used_by
    invite.used_at = now
    await session.flush()
    return invite


# ------------------------------------------------------------------------------- oauth states

OAUTH_STATE_MAX_AGE_S = 600


async def create_oauth_state(
    session: AsyncSession, user_id: int, provider: Provider | str, *, now: datetime
) -> OAuthState:
    row = OAuthState(
        state=secrets.token_urlsafe(32),
        user_id=user_id,
        provider=Provider(provider),
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def consume_oauth_state(
    session: AsyncSession, state: str, *, now: datetime, max_age_s: int = OAUTH_STATE_MAX_AGE_S
) -> OAuthState | None:
    """Single use: the row is deleted whether or not it is still valid. None if invalid/expired."""
    row = await session.get(OAuthState, state)
    if row is None:
        return None
    age = ensure_utc(now) - ensure_utc(row.created_at)
    await session.delete(row)
    await session.flush()
    if age > timedelta(seconds=max_age_s):
        return None
    return row


async def purge_oauth_states(
    session: AsyncSession, *, now: datetime, max_age_s: int = OAUTH_STATE_MAX_AGE_S
) -> int:
    cutoff = ensure_utc(now) - timedelta(seconds=max_age_s)
    result = await session.execute(sa.delete(OAuthState).where(OAuthState.created_at < cutoff))
    return _rowcount(result)
