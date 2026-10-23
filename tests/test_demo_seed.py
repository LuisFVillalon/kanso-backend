"""The demo seed: every feature populated, with about three months of history, the same each time."""

from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.crud.canvas_link_crud import sync_sample
from app.crud.demo_crud import HISTORY_DAYS, seed_demo_data
from app.crud.habit_crud import _max_streak_from_dates, _streak_ending_on
from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import USER_ID

TODAY = date(2026, 10, 20)


def _seed(db, today=TODAY):
    seed_demo_data(db, USER_ID, local_date=today.isoformat())


def _tasks(db) -> list[Task]:
    return db.query(Task).filter(Task.user_id == USER_ID).order_by(Task.id).all()


def test_open_tasks_cover_overdue_today_upcoming_and_undated(db):
    _seed(db)
    open_due = [t.due_date.date() if t.due_date else None for t in _tasks(db) if not t.completed]
    assert any(d and d < TODAY for d in open_due)
    assert any(d == TODAY for d in open_due)
    assert any(d and d > TODAY for d in open_due)
    assert None in open_due


def test_priorities_are_unique_among_active_tasks(db):
    _seed(db)
    priorities = [t.priority for t in _tasks(db) if t.priority is not None]
    assert len(priorities) >= 5
    assert len(set(priorities)) == len(priorities)


def test_linked_tasks_are_ordered_under_their_parents(db):
    _seed(db)
    children = [t for t in _tasks(db) if t.parent_task_id is not None]
    assert children
    by_parent: dict[int, list[int]] = {}
    for child in children:
        by_parent.setdefault(child.parent_task_id, []).append(child.position)
    assert len(by_parent) >= 2
    assert all(sorted(positions) == list(range(len(positions))) for positions in by_parent.values())


def test_canvas_linked_tasks_already_match_the_sample_assignments(db):
    _seed(db)
    linked = [t for t in _tasks(db) if t.canvas_assignment_id is not None]
    assert linked

    results = sync_sample(db, USER_ID, ZoneInfo("America/Los_Angeles"), TODAY)
    assert {r.task_id for r in results} == {t.id for t in linked}
    assert all(r.synced and not r.due_changed for r in results)


def test_completed_tasks_span_about_three_months(db):
    _seed(db)
    done = [t for t in _tasks(db) if t.completed]
    assert len(done) >= 100
    oldest = min(t.completed_date for t in done).date()
    assert TODAY - timedelta(days=HISTORY_DAYS) <= oldest < TODAY - timedelta(days=80)
    assert all(t.priority is None for t in done)
    # Every one of the last three months has completions, for the Reports month view.
    months = Counter((t.completed_date.year, t.completed_date.month) for t in done)
    assert {(2026, 8), (2026, 9), (2026, 10)} <= set(months)


def test_habit_streaks_match_their_logs_and_history_reaches_back(db):
    _seed(db)
    habits = db.query(Habit).filter(Habit.user_id == USER_ID).all()
    assert len(habits) >= 5
    for habit in habits:
        logged = {log.logged_date for log in db.query(HabitLog).filter(HabitLog.habit_id == habit.id)}
        assert logged and max(logged) <= TODAY
        newest = TODAY if TODAY in logged else TODAY - timedelta(days=1)
        assert habit.current_streak == _streak_ending_on(logged, newest)
        assert habit.max_streak == _max_streak_from_dates(logged)
        assert min(logged) < TODAY - timedelta(days=60)
    # Some kept up to today, some not yet today, and one broken streak.
    assert any(h.current_streak > 7 for h in habits)
    assert any(h.current_streak == 0 for h in habits)


def test_notes_have_sessions_spread_over_their_lifetime(db):
    _seed(db)
    notes = db.query(Note).filter(Note.user_id == USER_ID).all()
    assert len(notes) >= 10
    for note in notes:
        sessions = db.query(NoteSession).filter(NoteSession.note_id == note.id).all()
        assert sessions
        assert min(s.started_at for s in sessions) == note.created_date
        assert max(s.ended_at for s in sessions) == note.updated_date
    ended = [s.ended_at for s in db.query(NoteSession).filter(NoteSession.user_id == USER_ID)]
    assert min(ended) < datetime.combine(TODAY - timedelta(days=60), time.min)


def test_every_tag_is_used_and_some_span_tasks_notes_and_habits(db):
    _seed(db)
    tags = db.query(Tag).filter(Tag.user_id == USER_ID).all()
    assert len(tags) >= 6
    assert all(tag.tasks or tag.notes or tag.habits for tag in tags)
    assert any(tag.tasks and tag.notes and tag.habits for tag in tags)


def test_the_same_local_date_gives_the_same_data(db):
    def snapshot():
        tasks = [(t.title, t.due_date, t.completed) for t in _tasks(db)]
        logs = sorted(
            (h.title, log.logged_date)
            for h in db.query(Habit).filter(Habit.user_id == USER_ID)
            for log in h.logs
        )
        return tasks, logs

    _seed(db)
    first = snapshot()
    _seed(db)
    assert snapshot() == first
    _seed(db, TODAY + timedelta(days=1))
    assert snapshot() != first
