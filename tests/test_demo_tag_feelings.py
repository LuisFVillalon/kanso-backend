"""The demo's Reports › Mood › Patterns "Tags & feelings": energy and stress differ on Work and COMM 103 days.

A tag's day is one where a task with the tag was finished or a note with it
edited. Work days and days off are compared apart, a comparison needs 5 days
on each side, and a gap counts only when it is at least 0.5 and a seeded
reshuffle of the days rarely matches it (1 in 20, split across the tags).
"""

import random
import statistics
from collections import defaultdict
from datetime import UTC, date, timedelta

import pytest

from app.crud.demo_crud import _REST_DAYS, _TAGS, seed_demo_data
from app.models.mood_checkin_model import MoodCheckin
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import USER_ID

TODAY = date(2026, 10, 20)
MIN_DAYS = 5
MIN_GAP = 0.5
MAX_P = 0.05 / len(_TAGS)
SHUFFLES = 2000
FEELINGS = ("mood", "energy", "stress")


def _tag_days(db) -> dict[date, set[str]]:
    days: dict[date, set[str]] = defaultdict(set)
    finished = db.query(Task.completed_date, Tag.name).join(Task.tags).filter(
        Task.user_id == USER_ID, Task.completed_date.isnot(None)
    )
    edited = db.query(NoteSession.started_at, Tag.name).join(Note, Note.id == NoteSession.note_id).join(
        Note.tags
    ).filter(NoteSession.user_id == USER_ID)
    for at, name in [*finished, *edited]:
        days[(at if at.tzinfo is None else at.astimezone(UTC)).date()].add(name)
    return days


def _checkins(db) -> dict[date, MoodCheckin]:
    return {
        c.period_start: c
        for c in db.query(MoodCheckin).filter(MoodCheckin.user_id == USER_ID, MoodCheckin.period_kind == "day")
    }


def _chance(on: list[int], off: list[int], seed: str) -> float:
    """How often a reshuffle of the days into groups of the same sizes gives a gap at least as wide."""
    rng = random.Random(seed)
    gap = abs(statistics.mean(on) - statistics.mean(off))
    pool = on + off
    wider = 0
    for _ in range(SHUFFLES):
        rng.shuffle(pool)
        wider += abs(statistics.mean(pool[: len(on)]) - statistics.mean(pool[len(on):])) >= gap - 1e-9
    return (wider + 1) / (SHUFFLES + 1)


def _verdict(db, start: date, end: date, tag: str, feeling: str) -> str:
    """"higher", "lower", "same" or "thin" for a feeling on the tag's work days against the other work days."""
    tags = _tag_days(db)
    work_days = [
        c for day, c in _checkins(db).items() if start <= day <= end and day.isoweekday() % 7 not in _REST_DAYS
    ]
    on = [getattr(c, feeling) for c in work_days if tag in tags[c.period_start]]
    off = [getattr(c, feeling) for c in work_days if tag not in tags[c.period_start]]
    if len(on) < MIN_DAYS or len(off) < MIN_DAYS:
        return "thin"
    gap = statistics.mean(on) - statistics.mean(off)
    if abs(gap) < MIN_GAP or _chance(on, off, f"{tag}/{feeling}/{start}") >= MAX_P:
        return "same"
    return "higher" if gap > 0 else "lower"


def _month(first: date) -> tuple[date, date]:
    return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


# Past dates only: the seed never dates anything after the real now.
@pytest.mark.parametrize("today", [TODAY, date(2026, 8, 31), date(2026, 5, 15)])
def test_work_lowers_energy_and_comm_raises_stress(db, today):
    """Clear in the year and in the last full month, where every day has a check-in."""
    seed_demo_data(db, USER_ID, local_date=today.isoformat())
    yesterday = today - timedelta(days=1)
    periods = {
        "last month": _month((today.replace(day=1) - timedelta(days=1)).replace(day=1)),
        "year": (date(today.year, 1, 1), yesterday),
    }
    for name, (start, end) in periods.items():
        assert _verdict(db, start, end, "Work", "energy") == "lower", (today, name)
        assert _verdict(db, start, end, "COMM 103", "stress") == "higher", (today, name)


def test_what_a_visitor_sees_on_the_reference_date(db):
    """For 2026-10-20: the two seeded results in September, October so far and the year, and Personal with none."""
    seed_demo_data(db, USER_ID, local_date=TODAY.isoformat())
    september = _month(date(2026, 9, 1))
    october = (date(2026, 10, 1), TODAY - timedelta(days=1))
    year = (date(2026, 1, 1), TODAY - timedelta(days=1))
    for start, end in (september, year):
        assert _verdict(db, start, end, "Work", "energy") == "lower"
        assert _verdict(db, start, end, "COMM 103", "stress") == "higher"
        assert _verdict(db, start, end, "Work", "mood") == _verdict(db, start, end, "COMM 103", "mood") == "same"
    assert _verdict(db, *october, "Work", "energy") == "lower"
    assert {feeling: _verdict(db, *year, "Personal", feeling) for feeling in FEELINGS} == dict.fromkeys(
        FEELINGS, "same"
    )
