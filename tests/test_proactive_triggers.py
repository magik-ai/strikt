"""Every precondition in ``proactive.triggers`` with in-memory history facts (pure)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from strikt.core.clock import zone
from strikt.db.models import CoachingIntensity, Note, NoteKind, Reminder, ReminderStatus
from strikt.proactive import triggers as t
from strikt.proactive.stats import Streaks, WeekScorecard
from strikt.proactive.store import (
    DayKcal,
    HistoryFacts,
    MeasurementStatus,
    SameMealStreak,
    SkippedLunchStats,
    SleepNight,
    WorkoutComparison,
    WorkoutFacts,
)
from strikt.proactive.types import TriggerName
from tests.test_proactive_helpers import TODAY, TZ, make_ctx, make_profile, make_state

FRIDAY = date(2026, 9, 4)
MONDAY = date(2026, 9, 7)


def _day(
    d: date,
    kcal: float,
    *,
    meals: int = 3,
    flags: tuple[str, ...] = (),
    had_lunch: bool = True,
    evening: float = 600,
) -> DayKcal:
    return DayKcal(
        date=d,
        kcal=kcal,
        protein_g=150,
        fiber_g=20,
        meals=meals,
        flags=flags,
        had_lunch=had_lunch,
        evening_kcal=evening,
    )


def _night(end: date, onset: str, woke: str, asleep: float | None) -> SleepNight:
    oh, om = (int(x) for x in onset.split(":"))
    wh, wm = (int(x) for x in woke.split(":"))
    onset_day = end - timedelta(days=1) if oh >= 12 else end
    return SleepNight(
        night_of=end,
        started_local=datetime.combine(onset_day, time(oh, om), tzinfo=zone(TZ)),
        ended_local=datetime.combine(end, time(wh, wm), tzinfo=zone(TZ)),
        asleep_min=asleep,
        in_bed_min=None,
        performance_pct=None,
    )


def test_every_trigger_name_has_a_precondition() -> None:
    names: tuple[TriggerName, ...] = TriggerName.__args__  # type: ignore[attr-defined]
    assert set(t.PRECONDITIONS) == set(names)
    assert set(t.TRIGGER_CLASS) == set(names)
    for name in names:
        assert t.precondition(name).__name__ == f"check_{name}"


# ------------------------------------------------------------------------------- class A


def test_morning_line_fires_after_wake_with_yesterday_and_recovery() -> None:
    history = HistoryFacts(
        recent_days=(_day(TODAY - timedelta(days=1), 1910),),
        yesterday_verdict="Best structure this month.",
        sleep_nights=(_night(TODAY, "00:40", "08:35", 430),),
        measurements=(
            MeasurementStatus(
                type="waist",
                cadence_days=14,
                days_since=16,
                last_value=103,
                last_unit="cm",
                days_overdue=2,
            ),
        ),
    )
    fire = t.check_morning_line(make_state(recovery=87), make_ctx("08:15", history=history))
    assert fire is not None and fire.window_key == f"morning_line:{TODAY}"
    assert fire.facts["yesterday"]["kcal"] == 1910
    assert fire.facts["recovery"]["score"] == 87
    assert fire.facts["woke_minutes_late"] == 35
    assert fire.facts["measurements_overdue"][0]["type"] == "waist"
    assert t.check_morning_line(None, make_ctx("07:00", history=history)) is None


def test_no_first_meal_fires_at_wake_plus_3h_with_skipped_lunch_facts() -> None:
    stats = SkippedLunchStats(
        skipped_days=3,
        with_lunch_days=10,
        avg_evening_kcal_skipped=1650,
        avg_evening_kcal_with_lunch=700,
        avg_day_kcal_skipped=2600,
        avg_day_kcal_with_lunch=1950,
    )
    ctx = make_ctx("11:05", history=HistoryFacts(skipped_lunch=stats))
    fire = t.check_no_first_meal(make_state(), ctx)
    assert fire is not None and fire.klass == "time"
    assert fire.facts["hours_since_wake"] == 3.1
    assert fire.facts["avg_day_kcal_after_skipped_lunch"] == 2600
    assert fire.facts["wake_time"] == "08:00"
    # before the deadline, with a meal, closed or unknown: silent
    assert t.check_no_first_meal(make_state(), make_ctx("10:59")) is None
    assert t.check_no_first_meal(make_state(meals=[("09:00", 400, 30, "breakfast")]), ctx) is None
    assert t.check_no_first_meal(make_state(closed=True), ctx) is None
    assert t.check_no_first_meal(None, ctx) is None


def test_no_lunch_by_15() -> None:
    breakfast_only = make_state(meals=[("09:00", 400, 30, "breakfast")])
    fire = t.check_no_lunch(breakfast_only, make_ctx("15:00"))
    assert fire is not None and fire.facts["first_meal_at"] == "09:00"
    assert fire.facts["protein_so_far"] == 30
    assert t.check_no_lunch(breakfast_only, make_ctx("14:59")) is None
    with_lunch = make_state(meals=[("09:00", 400, 30, "breakfast"), ("13:10", 600, 50, "unknown")])
    assert t.check_no_lunch(with_lunch, make_ctx("15:00")) is None
    tagged = make_state(meals=[("16:30", 600, 50, "lunch")])
    assert t.check_no_lunch(tagged, make_ctx("17:00")) is None


def test_fiber_check_under_a_third_of_target_by_1330() -> None:
    low = make_state(meals=[("09:00", 400, 30, "breakfast")], fiber=4)
    fire = t.check_fiber_check(low, make_ctx("13:30"))
    assert (
        fire is not None and fire.facts["fiber_threshold"] == 10 and fire.facts["fiber_so_far"] == 4
    )
    assert fire.facts["fiber_gap_to_target"] == 26
    assert t.check_fiber_check(low, make_ctx("13:00")) is None
    enough = make_state(meals=[("09:00", 400, 30, "breakfast")], fiber=12)
    assert t.check_fiber_check(enough, make_ctx("13:30")) is None


def test_protein_check_under_150_by_18() -> None:
    state = make_state(meals=[("09:00", 400, 36, "breakfast"), ("13:00", 700, 60, "lunch")])
    fire = t.check_protein_check(state, make_ctx("18:00"))
    assert fire is not None
    assert fire.facts["protein_so_far"] == 96 and fire.facts["protein_threshold"] == 147
    assert fire.facts["dinner_protein_needed"] == 114
    assert t.check_protein_check(state, make_ctx("17:30")) is None
    high = make_state(meals=[("09:00", 400, 80, "breakfast"), ("13:00", 700, 80, "lunch")])
    assert t.check_protein_check(high, make_ctx("18:00")) is None


def test_no_dinner_and_day_not_closed() -> None:
    state = make_state(meals=[("09:00", 400, 36, "breakfast"), ("13:00", 700, 60, "lunch")])
    fire = t.check_no_dinner(state, make_ctx("21:00"))
    assert fire is not None and fire.facts["last_meal_at"] == "13:00"
    assert t.check_no_dinner(state, make_ctx("20:30")) is None
    dinner = make_state(meals=[("19:30", 700, 60, "unknown")])
    assert t.check_no_dinner(dinner, make_ctx("21:00")) is None

    fire2 = t.check_day_not_closed(dinner, make_ctx("23:00"))
    assert (
        fire2 is not None
        and fire2.facts["dinner_logged"] is True
        and fire2.facts["bed_time"] == "00:30"
    )
    assert t.check_day_not_closed(dinner, make_ctx("22:00")) is None
    assert t.check_day_not_closed(make_state(closed=True), make_ctx("23:00")) is None


def test_bedtime_minus_30_windows_on_the_evening_date() -> None:
    history = HistoryFacts(sleep_nights=(_night(TODAY, "01:10", "08:00", 380),))
    fire = t.check_bedtime_minus_30(make_state(), make_ctx("00:00", history=history))
    assert fire is not None and fire.facts["bed_time"] == "00:30"
    assert fire.window_key == f"bedtime_minus_30:{TODAY - timedelta(days=1)}"
    assert fire.facts["avg_asleep_min_3n"] == 380
    late_evening = t.check_bedtime_minus_30(None, make_ctx("23:59", history=history))
    assert late_evening is not None and late_evening.window_key == f"bedtime_minus_30:{TODAY}"
    assert t.check_bedtime_minus_30(None, make_ctx("21:00")) is None


def test_wake_check_from_sleep_event_payload() -> None:
    history = HistoryFacts(
        sleep_nights=(
            _night(TODAY, "00:50", "08:50", 420),
            _night(TODAY - timedelta(days=1), "01:00", "08:45", 400),
            _night(TODAY - timedelta(days=2), "00:30", "08:05", 430),
        )
    )
    payload = {"ended_at": datetime(2026, 9, 3, 4, 50, tzinfo=UTC).isoformat()}  # 08:50 Dubai
    fire = t.check_wake_check(None, make_ctx("09:00", history=history, payload=payload))
    assert fire is not None
    assert fire.facts["minutes_late"] == 50 and fire.facts["late_days_in_a_row"] == 2
    assert fire.facts["proposed_bed_time"] == "23:40"
    on_time = {"ended_at": datetime(2026, 9, 3, 4, 10, tzinfo=UTC).isoformat()}
    assert t.check_wake_check(None, make_ctx("09:00", history=history, payload=on_time)) is None
    # no payload: falls back to the latest night when it ended today
    assert t.check_wake_check(None, make_ctx("09:00", history=history)) is not None


def test_measurement_overdue_at_16_days() -> None:
    history = HistoryFacts(
        measurements=(
            MeasurementStatus(
                type="waist",
                cadence_days=14,
                days_since=16,
                last_value=103,
                last_unit="cm",
                days_overdue=2,
            ),
            MeasurementStatus(
                type="weight",
                cadence_days=7,
                days_since=2,
                last_value=104.2,
                last_unit="kg",
                days_overdue=0,
            ),
        )
    )
    fire = t.check_measurement_overdue(None, make_ctx("08:05", history=history))
    assert fire is not None
    assert (
        fire.facts["type"] == "waist"
        and fire.facts["days_since"] == 16
        and fire.facts["days_overdue"] == 2
    )
    assert fire.facts["kpi"] == "waist" and fire.facts["kpi_target"] == 94
    assert len(fire.facts["overdue"]) == 1
    fresh = HistoryFacts(
        measurements=(
            MeasurementStatus(
                type="waist",
                cadence_days=14,
                days_since=3,
                last_value=103,
                last_unit="cm",
                days_overdue=0,
            ),
        )
    )
    assert t.check_measurement_overdue(None, make_ctx("08:05", history=fresh)) is None


def test_weekly_review_uses_the_scorecard() -> None:
    card = WeekScorecard(
        week_start=date(2026, 8, 31),
        days_logged=6,
        days_closed=5,
        days_within_target=4,
        avg_kcal=1980,
        avg_protein_g=190,
        avg_fiber_g=24,
        sessions=2,
        bedtime_hits=3,
        nights_tracked=6,
        measurements_taken=1,
    )
    fire = t.check_weekly_review(
        None, make_ctx("20:00", day=date(2026, 9, 6), history=HistoryFacts(scorecard=card))
    )
    assert fire is not None and fire.window_key == "weekly_review:2026-08-31"
    assert fire.facts["scorecard"]["avg_protein_g"] == 190 and fire.facts["sessions_planned"] == 3
    assert t.check_weekly_review(None, make_ctx("20:00")) is None


def test_silence_check_after_24h() -> None:
    last = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)  # 30 h before 12:00 Dubai on the 3rd
    fire = t.check_silence_check(
        None, make_ctx("12:00", history=HistoryFacts(last_user_message_at=last, unanswered_sends=2))
    )
    assert (
        fire is not None
        and fire.facts["hours_silent"] == 30
        and fire.facts["unanswered_nudges_today"] == 2
    )
    recent = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    assert (
        t.check_silence_check(
            None, make_ctx("12:00", history=HistoryFacts(last_user_message_at=recent))
        )
        is None
    )
    assert t.check_silence_check(None, make_ctx("12:00")) is None


def test_escalation_followup_delegates_to_the_parent() -> None:
    ctx = make_ctx(
        "11:30", payload={"parent": "no_first_meal", "window_key": f"no_first_meal:{TODAY}"}
    )
    fire = t.check_escalation_followup(make_state(), ctx)
    assert (
        fire is not None
        and fire.name == "no_first_meal"
        and fire.window_key == f"no_first_meal:{TODAY}"
    )
    assert (
        t.check_escalation_followup(make_state(meals=[("09:00", 400, 30, "breakfast")]), ctx)
        is None
    )
    assert (
        t.check_escalation_followup(make_state(), make_ctx("11:30", payload={"parent": "nope"}))
        is None
    )
    assert (
        t.check_escalation_followup(
            make_state(), make_ctx("11:30", payload={"parent": "escalation_followup"})
        )
        is None
    )


# ------------------------------------------------------------------------------- class B


def test_whoop_workout_synced_compares_with_previous_and_average() -> None:
    previous = WorkoutFacts.from_values(
        sport="weightlifting",
        started_at=datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        tz=TZ,
        duration_min=45,
        kcal=406,
        avg_hr=130,
        max_hr=165,
        strain=12.1,
        zones_min={"z0": 5, "z1": 10, "z2": 20, "z3": 10},
    )
    comparison = WorkoutComparison(
        previous=previous,
        avg_count=9,
        avg_duration_min=55,
        avg_kcal=380,
        avg_avg_hr=118,
        avg_strain=10.4,
    )
    payload = {
        "sport": "weightlifting",
        "external_id": "w-1",
        "started_at": datetime(2026, 9, 3, 6, 0, tzinfo=UTC).isoformat(),
        "ended_at": datetime(2026, 9, 3, 7, 34, tzinfo=UTC).isoformat(),
        "duration_min": 94,
        "kcal": 361,
        "avg_hr": 104,
        "max_hr": 150,
        "strain": 8.2,
        "zones_min": {"z0": 54.5, "z1": 20, "z2": 15, "z3": 4.5},
    }
    fire = t.check_whoop_workout_synced(
        None,
        make_ctx("11:40", history=HistoryFacts(workout_comparison=comparison), payload=payload),
    )
    assert (
        fire is not None and fire.window_key == "whoop_workout_synced:w-1" and fire.klass == "data"
    )
    assert fire.facts["this"]["zone0_pct"] == 58 and fire.facts["this"]["kcal_per_min"] == 3.8
    assert fire.facts["previous_same_sport"]["kcal"] == 406
    assert fire.facts["deltas_vs_previous"]["avg_hr"] == -26
    assert fire.facts["deltas_vs_avg_30d"]["duration_min"] == 39
    assert fire.facts["ended_within_2h_of_bed"] is False
    assert t.check_whoop_workout_synced(None, make_ctx("11:40", payload={"sport": "run"})) is None


def test_whoop_recovery_low_and_high() -> None:
    low = t.check_whoop_recovery_low(
        None, make_ctx("08:30", payload={"score": 21, "rhr": 58, "hrv_ms": 31})
    )
    assert (
        low is not None
        and low.facts["score"] == 21
        and low.facts["training_planned_today"] is False
    )
    assert t.check_whoop_recovery_low(None, make_ctx("08:30", payload={"score": 55})) is None
    assert t.check_whoop_recovery_low(make_state(recovery=30), make_ctx("08:30")) is not None

    bad_streak = HistoryFacts(
        recent_recoveries=(
            (TODAY - timedelta(days=3), 33.0),
            (TODAY - timedelta(days=2), 41.0),
            (TODAY - timedelta(days=1), 45.0),
        )
    )
    high = t.check_whoop_recovery_high(
        None, make_ctx("08:30", history=bad_streak, payload={"score": 87})
    )
    assert high is not None and high.facts["prior_scores"] == [33, 41, 45]
    fine_streak = HistoryFacts(
        recent_recoveries=((TODAY - timedelta(days=2), 70.0), (TODAY - timedelta(days=1), 75.0))
    )
    assert (
        t.check_whoop_recovery_high(
            None, make_ctx("08:30", history=fine_streak, payload={"score": 87})
        )
        is None
    )
    assert (
        t.check_whoop_recovery_high(
            None, make_ctx("08:30", history=bad_streak, payload={"score": 60})
        )
        is None
    )


def test_whoop_no_workout_uses_the_plan_gap() -> None:
    last = WorkoutFacts.from_values(
        sport="weightlifting",
        started_at=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
        tz=TZ,
        duration_min=60,
        kcal=400,
        avg_hr=120,
        max_hr=160,
        strain=10,
        zones_min=None,
    )
    fire = t.check_whoop_no_workout(
        None, make_ctx("10:00", history=HistoryFacts(last_workout=last))
    )
    assert (
        fire is not None
        and fire.facts["days_since_last"] == 6
        and fire.facts["allowed_gap_days"] == 4
    )
    assert fire.facts["training_days"] == ["mon", "wed", "fri"]
    recent = WorkoutFacts.from_values(
        sport="run",
        started_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
        tz=TZ,
        duration_min=30,
        kcal=300,
        avg_hr=140,
        max_hr=170,
        strain=9,
        zones_min=None,
    )
    assert (
        t.check_whoop_no_workout(None, make_ctx("10:00", history=HistoryFacts(last_workout=recent)))
        is None
    )
    assert t.check_whoop_no_workout(None, make_ctx("10:00")) is None


def test_scale_weight_received_trend_only_and_water() -> None:
    weights = tuple(
        (datetime.combine(TODAY - timedelta(days=n), time(7, 30), tzinfo=zone(TZ)), 104.0 + n * 0.2)
        for n in (3, 2, 1)
    )
    payload = {
        "value": 105.1,
        "unit": "kg",
        "measured_at": datetime(2026, 9, 3, 3, 30, tzinfo=UTC).isoformat(),
    }
    fire = t.check_scale_weight_received(
        None, make_ctx("08:00", history=HistoryFacts(recent_weights=weights), payload=payload)
    )
    assert fire is not None and fire.facts["readings_7d"] == 3 and fire.facts["avg_7d"] == 104.4
    assert fire.facts["delta_vs_avg_7d"] == 0.7 and fire.facts["likely_water"] is False
    # a single prior reading and no flag: no comment on a single reading
    assert (
        t.check_scale_weight_received(
            None,
            make_ctx("08:00", history=HistoryFacts(recent_weights=weights[:1]), payload=payload),
        )
        is None
    )
    salty = HistoryFacts(
        recent_days=(_day(TODAY - timedelta(days=1), 2100, flags=("salty", "alcohol")),)
    )
    water = t.check_scale_weight_received(None, make_ctx("08:00", history=salty, payload=payload))
    assert (
        water is not None
        and water.facts["likely_water"] is True
        and water.facts["yesterday_flags"] == ["salty", "alcohol"]
    )
    assert t.check_scale_weight_received(None, make_ctx("08:00", payload={})) is None


def test_sleep_debt_three_nights_under_target() -> None:
    nights = (
        _night(TODAY, "01:10", "07:40", 360),
        _night(TODAY - timedelta(days=1), "01:30", "07:50", 350),
        _night(TODAY - timedelta(days=2), "00:50", "07:30", 370),
    )
    fire = t.check_sleep_debt_accumulating(
        None, make_ctx("08:45", history=HistoryFacts(sleep_nights=nights, sleep_target_min=420))
    )
    assert (
        fire is not None
        and fire.facts["total_deficit_min"] == 180
        and len(fire.facts["nights"]) == 3
    )
    assert fire.facts["proposed_bed_time"] == "00:00"
    one_good = (nights[0], _night(TODAY - timedelta(days=1), "00:20", "08:00", 440), nights[2])
    assert (
        t.check_sleep_debt_accumulating(
            None,
            make_ctx("08:45", history=HistoryFacts(sleep_nights=one_good, sleep_target_min=420)),
        )
        is None
    )
    assert (
        t.check_sleep_debt_accumulating(
            None,
            make_ctx("08:45", history=HistoryFacts(sleep_nights=nights[:2], sleep_target_min=420)),
        )
        is None
    )


def test_sleep_onset_late() -> None:
    payload = {"started_at": datetime(2026, 9, 2, 21, 15, tzinfo=UTC).isoformat()}  # 01:15 Dubai
    fire = t.check_sleep_onset_late(None, make_ctx("08:30", payload=payload))
    assert fire is not None and fire.facts["onset"] == "01:15" and fire.facts["minutes_late"] == 45
    early = {"started_at": datetime(2026, 9, 2, 20, 40, tzinfo=UTC).isoformat()}  # 00:40
    assert t.check_sleep_onset_late(None, make_ctx("08:30", payload=early)) is None
    assert t.check_sleep_onset_late(None, make_ctx("08:30")) is None


# ------------------------------------------------------------------------------- class C


def test_weekend_risk_with_a_past_weekend_blowup() -> None:
    blowup = _day(date(2026, 8, 22), 2650, flags=("alcohol",), had_lunch=False, evening=1800)
    fire = t.check_weekend_risk(
        None, make_ctx("17:00", day=FRIDAY, history=HistoryFacts(weekend_blowups=(blowup,)))
    )
    assert fire is not None and fire.klass == "pattern"
    assert (
        fire.facts["past_blowups"][0]["weekday"] == "Sat"
        and fire.facts["avg_over_target_kcal"] == 650
    )
    assert t.check_weekend_risk(None, make_ctx("17:00", day=FRIDAY)) is None
    assert (
        t.check_weekend_risk(
            None, make_ctx("17:00", day=TODAY, history=HistoryFacts(weekend_blowups=(blowup,)))
        )
        is None
    )


def test_two_off_days_in_a_row() -> None:
    days = (
        _day(MONDAY - timedelta(days=3), 1900),
        _day(MONDAY - timedelta(days=2), 2600),
        _day(MONDAY - timedelta(days=1), 2450),
    )
    fire = t.check_two_off_days(
        None, make_ctx("09:00", day=MONDAY, history=HistoryFacts(recent_days=days))
    )
    assert fire is not None and fire.facts["over_by_kcal"] == [600, 450]
    assert fire.facts["off_days"][0]["weekday"] == "Sat"
    one_off = (_day(MONDAY - timedelta(days=2), 1900), _day(MONDAY - timedelta(days=1), 2450))
    assert (
        t.check_two_off_days(
            None, make_ctx("09:00", day=MONDAY, history=HistoryFacts(recent_days=one_off))
        )
        is None
    )
    flagged = (
        _day(MONDAY - timedelta(days=2), 1500, flags=("off",)),
        _day(MONDAY - timedelta(days=1), 1600, flags=("off",)),
    )
    assert (
        t.check_two_off_days(
            None, make_ctx("09:00", day=MONDAY, history=HistoryFacts(recent_days=flagged))
        )
        is not None
    )
    stale = (_day(MONDAY - timedelta(days=4), 2600), _day(MONDAY - timedelta(days=3), 2450))
    assert (
        t.check_two_off_days(
            None, make_ctx("09:00", day=MONDAY, history=HistoryFacts(recent_days=stale))
        )
        is None
    )


def test_same_meal_streak_event_planned_post_travel() -> None:
    streak = SameMealStreak(name="chicken breast", days=5, last_date=TODAY)
    fire = t.check_same_meal_streak(None, make_ctx("12:05", history=HistoryFacts(same_meal=streak)))
    assert (
        fire is not None
        and fire.facts["item"] == "chicken breast"
        and fire.window_key == f"same_meal_streak:chicken_breast:{TODAY}"
    )
    assert t.check_same_meal_streak(None, make_ctx("12:05")) is None

    note = Note(
        user_id=1,
        kind=NoteKind.event,
        text="Ramen at Kinoya for lunch",
        confidence=0.9,
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
        active=True,
    )
    planned = t.check_event_planned(make_state(), make_ctx("08:20", notes=[note]))
    assert planned is not None and planned.facts["events"][0]["text"].startswith("Ramen")
    with_plan = t.check_event_planned(make_state(plan={"lunch": "ramen"}), make_ctx("08:20"))
    assert with_plan is not None and with_plan.facts["plan"] == {"lunch": "ramen"}
    assert t.check_event_planned(make_state(), make_ctx("08:20")) is None
    # "Date night Saturday" stored as the coach prompt prescribes (set_day_flag planned_indulgence)
    flagged = t.check_event_planned(make_state(flags=["planned_indulgence"]), make_ctx("08:20"))
    assert flagged is not None and flagged.facts["planned_indulgence"] is True

    travel = (
        _day(TODAY - timedelta(days=3), 2200, flags=("travel",)),
        _day(TODAY - timedelta(days=2), 2300, flags=("travel",)),
        _day(TODAY - timedelta(days=1), 2100, flags=("travel",)),
    )
    back = t.check_post_travel_reentry(
        make_state(), make_ctx("08:20", history=HistoryFacts(recent_days=travel))
    )
    assert back is not None and back.facts["travel_days"] == 3
    assert (
        t.check_post_travel_reentry(
            make_state(flags=["travel"]),
            make_ctx("08:20", history=HistoryFacts(recent_days=travel)),
        )
        is None
    )
    assert t.check_post_travel_reentry(make_state(), make_ctx("08:20")) is None


def test_clean_streak_every_third_day_once() -> None:
    three = HistoryFacts(
        streaks=Streaks(clean_days=3, clean_streak_start=TODAY - timedelta(days=3))
    )
    fire = t.check_clean_streak(None, make_ctx("09:00", history=three))
    assert (
        fire is not None
        and fire.facts["clean_days"] == 3
        and fire.facts["next_weekend_in_days"] == 2
    )
    assert fire.window_key == f"clean_streak:{TODAY - timedelta(days=3)}:3"
    assert (
        t.check_clean_streak(
            None, make_ctx("09:00", history=HistoryFacts(streaks=Streaks(clean_days=4)))
        )
        is None
    )
    assert (
        t.check_clean_streak(
            None, make_ctx("09:00", history=HistoryFacts(streaks=Streaks(clean_days=6)))
        )
        is not None
    )
    assert t.check_clean_streak(None, make_ctx("09:00")) is None


def test_intensity_restored_after_the_deadline() -> None:
    expired = make_profile(
        temp_intensity=CoachingIntensity.gentle,
        temp_intensity_until=datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
    )
    fire = t.check_intensity_restored(None, make_ctx("09:00", profile=expired))
    assert (
        fire is not None
        and fire.facts["temporary_intensity"] == "gentle"
        and fire.facts["restored_intensity"] == "pushy"
    )
    assert fire.window_key == "intensity_restored:2026-09-03"
    still = make_profile(
        temp_intensity=CoachingIntensity.gentle,
        temp_intensity_until=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert t.check_intensity_restored(None, make_ctx("09:00", profile=still)) is None
    assert t.check_intensity_restored(None, make_ctx("09:00")) is None


def test_reminder_due_from_payload_and_from_pending_rows() -> None:
    payload = {
        "reminder_id": 7,
        "text": "waist, fasted",
        "due_at": datetime(2026, 9, 3, 4, 0, tzinfo=UTC).isoformat(),
    }
    fire = t.check_reminder_due(None, make_ctx("08:01", payload=payload))
    assert (
        fire is not None and fire.window_key == "reminder_due:7" and fire.facts["due_at"] == "08:00"
    )
    assert fire.payload == {"reminder_id": 7, "text": "waist, fasted"}
    row = Reminder(
        id=9,
        user_id=1,
        due_at=datetime(2026, 9, 3, 3, 0, tzinfo=UTC),
        text="call doc",
        kind="custom",
        status=ReminderStatus.pending,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    from_rows = t.check_reminder_due(
        None, make_ctx("08:01", history=HistoryFacts(pending_reminders=(row,)))
    )
    assert from_rows is not None and from_rows.window_key == "reminder_due:9"
    assert t.check_reminder_due(None, make_ctx("08:01")) is None


@pytest.mark.parametrize(
    ("name", "klass"),
    [
        ("no_lunch", "time"),
        ("whoop_workout_synced", "data"),
        ("weekend_risk", "pattern"),
        ("reminder_due", "pattern"),
    ],
)
def test_trigger_classes(name: TriggerName, klass: str) -> None:
    assert t.TRIGGER_CLASS[name] == klass


def test_checkin_deadlines_drive_the_silence_preconditions() -> None:
    profile = make_profile(checkin_times=["13:00", "20:00"])
    assert t.checkin_deadlines(profile) == {"no_lunch": time(13, 0), "no_dinner": time(20, 0)}
    assert t.checkin_deadlines(make_profile()) == {}
    assert t.checkin_deadlines(None) == {}
    empty = make_state()
    # default: nothing before 15:00; with a 13:00 check-in the lunch ping is due at 13:00
    assert t.check_no_lunch(empty, make_ctx("13:05")) is None
    assert t.check_no_lunch(empty, make_ctx("13:05", profile=profile)) is not None
    assert t.check_no_lunch(empty, make_ctx("12:30", profile=profile)) is None
    assert t.check_no_dinner(empty, make_ctx("20:05", profile=profile)) is not None
    assert t.check_no_dinner(empty, make_ctx("20:05")) is None


def test_sick_and_travel_days_pause_the_meal_pressure() -> None:
    for flag in ("sick", "travel"):
        flagged = make_state(flags=[flag])
        assert t.check_no_lunch(flagged, make_ctx("15:05")) is None
        assert t.check_protein_check(flagged, make_ctx("18:05")) is None
        assert t.check_fiber_check(flagged, make_ctx("13:35")) is None
        assert t.check_no_first_meal(flagged, make_ctx("11:05")) is None
    assert t.check_no_lunch(make_state(), make_ctx("15:05")) is not None
