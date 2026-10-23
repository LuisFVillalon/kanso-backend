"""The demo's Reports › Mood › Patterns: each month shows the link it was drawn for.

"Mood & your activity" labels how mood moved with tasks finished, minutes in
notes and habits checked off. The rules below are a line-by-line port of the
frontend's, so these tests see the same labels a demo visitor would:
kanso-frontend src/app/utils/calendarReport.ts (ranks, pearson, spearman,
normalCdf, rhoP, analyseCorrelation, metricValue, dailyActivity) and
PATTERN_MIN_DAYS in src/app/utils/moodReport.ts, as of main 0ef3b01.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest

from app.crud.demo_crud import _plan_history, seed_demo_data
from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.mood_checkin_model import MoodCheckin
from app.models.note_session_model import NoteSession
from app.models.task_model import Task
from tests.conftest import USER_ID

TODAY = date(2026, 10, 20)

# ── Port of the frontend's rules ─────────────────────────────────────────────
PATTERN_MIN_DAYS = 10
MAX_P = 0.1
MIN_RHO = 0.15
EARLY_SIGN_DAYS = 20
STRONG_MIN_DAYS = 30
METRICS = ("tasks", "notes", "habits")


def _js_round(x: float) -> int:
    """Math.round: halves go up, unlike Python's round()."""
    return math.floor(x + 0.5)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sx = sum((a - mx) ** 2 for a in x)
    sy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sx * sy) if sx and sy else 0.0


def _spearman(x: list[float], y: list[float]) -> float:
    return 0.0 if len(x) < 2 else _pearson(_ranks(x), _ranks(y))


def _normal_cdf(z: float) -> float:
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    tail = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - tail if z > 0 else tail


def _rho_p(rho: float, n: int) -> float:
    clamped = max(-0.999, min(0.999, rho))
    z = math.atanh(clamped) * math.sqrt((n - 3) / 1.06)
    return 2 * (1 - _normal_cdf(abs(z)))


@dataclass
class Result:
    n: int
    level: str
    rho: float | None

    @property
    def direction(self) -> str:
        """"higher" or "lower": how mood moved on days with more of the activity."""
        return "higher" if self.rho > 0 else "lower"

    @property
    def hint(self) -> bool:
        """Whether the sentence ends "…, so treat it as a hint."."""
        return self.level in ("slight", "moderate", "strong") and self.n < STRONG_MIN_DAYS


def analyse(points: list[tuple[int, int]]) -> Result:
    """analyseCorrelation over (mood, value) points."""
    n = len(points)
    if n < PATTERN_MIN_DAYS:
        return Result(n, "insufficient", None)
    xs = [value for _, value in points]
    if len(set(xs)) < 3:
        return Result(n, "flat", None)

    rho = _spearman(xs, [mood for mood, _ in points])
    p = _rho_p(rho, n)

    ordered = sorted(xs)
    threshold = ordered[n // 2]
    if threshold == ordered[0]:
        threshold = next(v for v in ordered if v > threshold)
    high = [mood for mood, value in points if value >= threshold]
    low = [mood for mood, value in points if value < threshold]
    split_sign = _sign(_js_round(sum(high) / len(high) * 10) - _js_round(sum(low) / len(low) * 10))

    strength = abs(rho)
    if p > MAX_P or strength < MIN_RHO or split_sign != _sign(rho):
        level = "none"
    else:
        level = "slight" if strength < 0.3 else "moderate" if strength < 0.5 else "strong"
    if level != "none" and n < EARLY_SIGN_DAYS:
        level = "early"
    elif level == "strong" and n < STRONG_MIN_DAYS:
        level = "moderate"
    return Result(n, level, rho)


# ── Periods ──────────────────────────────────────────────────────────────────
# A day's (mood or None, tasks, note minutes, habits).
Days = dict[date, tuple[int | None, int, int, int]]


def _month(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return first, following - timedelta(days=1)


def _months_ago(today: date, months: int) -> tuple[date, date]:
    first = today.replace(day=1)
    for _ in range(months):
        first = (first - timedelta(days=1)).replace(day=1)
    return _month(first)


def _year(today: date) -> tuple[date, date]:
    return date(today.year, 1, 1), date(today.year, 12, 31)


def _period(days: Days, today: date, start: date, end: date) -> dict[str, Result]:
    """Each metric's result for a report period: one point per day up to today with a daily check-in."""
    checked = [values for day, values in sorted(days.items()) if start <= day <= min(end, today) and values[0]]
    return {
        metric: analyse([(values[0], values[i + 1]) for values in checked])
        for i, metric in enumerate(METRICS)
    }


def _planned(today: date) -> Days:
    return {d.day: (d.mood, d.tasks, d.note_minutes, d.habits) for d in _plan_history(today)}


def _summary(results: dict[str, Result]) -> dict[str, tuple[str, str | None, bool]]:
    return {
        metric: (r.level, r.direction if r.rho is not None and r.level != "none" else None, r.hint)
        for metric, r in results.items()
    }


# ── The demo's results ───────────────────────────────────────────────────────

def _check_intended_links(days: Days, today: date) -> None:
    # Last month: every day checked in. Tasks a strong link, mood higher on
    # fuller days (moderate, as a hint, in a 28-day February); note time a
    # moderate link the other way; habits none.
    last = _period(days, today, *_months_ago(today, 1))
    n = last["tasks"].n
    assert n == _months_ago(today, 1)[1].day
    tasks = ("strong", "higher", False) if n >= STRONG_MIN_DAYS else ("moderate", "higher", True)
    assert _summary(last) == {
        "tasks": tasks,
        "notes": ("moderate", "lower", n < STRONG_MIN_DAYS),
        "habits": ("none", None, False),
    }, today

    # The month before: 24 check-ins. Habits a moderate link, flagged as a hint.
    before = _period(days, today, *_months_ago(today, 2))
    assert _summary(before) == {
        "tasks": ("none", None, False),
        "notes": ("none", None, False),
        "habits": ("moderate", "higher", True),
    }, today

    # This month: tasks an early sign while under 20 check-ins, a firmer link after.
    this = _period(days, today, *_months_ago(today, 0))
    n = this["tasks"].n
    if n < PATTERN_MIN_DAYS:
        assert {r.level for r in this.values()} == {"insufficient"}, today
    else:
        assert this["tasks"].level == ("early" if n < EARLY_SIGN_DAYS else "moderate" if n < 30 else "strong"), today
        assert this["tasks"].direction == "higher", today
        assert this["notes"].level == this["habits"].level == "none", today

    # The year, once it holds the last two months: note time a slight link, mood lower with more.
    if today.month >= 3:
        year = _period(days, today, *_year(today))
        assert (year["notes"].level, year["notes"].direction) == ("slight", "lower"), today


@pytest.mark.parametrize("year", [2026, 2028])
def test_every_local_date_shows_the_intended_links(year):
    """The history is redrawn for each visitor's local date; every date of the year keeps the intended results."""
    day = date(year, 1, 1)
    while day.year == year:
        _check_intended_links(_planned(day), day)
        day += timedelta(days=1)


def _seeded(db, today: date) -> Days:
    """Per-day mood and activity read back from the seeded rows, counted as the frontend's dailyActivity does
    (the browser's local time taken as UTC, which the seed's timestamps are in)."""
    tasks: dict[date, int] = defaultdict(int)
    for (completed,) in db.query(Task.completed_date).filter(Task.user_id == USER_ID, Task.completed_date.isnot(None)):
        tasks[completed.date()] += 1

    seconds: dict[date, float] = defaultdict(float)
    for started, ended in db.query(NoteSession.started_at, NoteSession.ended_at).filter(NoteSession.user_id == USER_ID):
        cursor = started
        while cursor < ended:
            piece_end = min(ended, datetime.combine(cursor.date() + timedelta(days=1), time.min))
            seconds[cursor.date()] += (piece_end - cursor).total_seconds()
            cursor = piece_end

    habits: dict[date, int] = defaultdict(int)
    logs = db.query(HabitLog.logged_date).join(Habit).filter(Habit.user_id == USER_ID)
    for (logged,) in logs:
        habits[logged] += 1

    moods = {
        c.period_start: c.mood
        for c in db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID, MoodCheckin.period_kind == "day")
    }
    every_day = set(tasks) | set(seconds) | set(habits) | set(moods)
    return {
        day: (moods.get(day), tasks[day], _js_round(seconds[day] / 60), habits[day])
        for day in every_day
        if day < today
    }


def test_the_seeded_rows_give_the_planned_days(db):
    seed_demo_data(db, USER_ID, local_date=TODAY.isoformat())
    seeded = _seeded(db, TODAY)
    planned = _planned(TODAY)
    assert {day: values for day, values in seeded.items() if values != (None, 0, 0, 0)} == {
        day: values for day, values in planned.items() if values != (None, 0, 0, 0)
    }


def test_what_a_visitor_sees_on_the_reference_date(db):
    """The results named in the PR, read from the seeded rows for 2026-10-20."""
    seed_demo_data(db, USER_ID, local_date=TODAY.isoformat())
    days = _seeded(db, TODAY)
    _check_intended_links(days, TODAY)

    assert _summary(_period(days, TODAY, date(2026, 9, 1), date(2026, 9, 30))) == {
        "tasks": ("strong", "higher", False),
        "notes": ("moderate", "lower", False),
        "habits": ("none", None, False),
    }
    assert _summary(_period(days, TODAY, date(2026, 8, 1), date(2026, 8, 31))) == {
        "tasks": ("none", None, False),
        "notes": ("none", None, False),
        "habits": ("moderate", "higher", True),
    }
    october = _period(days, TODAY, date(2026, 10, 1), date(2026, 10, 31))
    assert (october["tasks"].level, october["tasks"].direction) == ("early", "higher")
    july = _period(days, TODAY, date(2026, 7, 1), date(2026, 7, 31))
    assert {r.level for r in july.values()} == {"insufficient"}
    year = _period(days, TODAY, date(2026, 1, 1), date(2026, 12, 31))
    assert (year["notes"].level, year["notes"].direction) == ("slight", "lower")
