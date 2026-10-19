"""Archived tasks, notes and habits stay out of the debrief, note-time totals, streak checks and stats."""

from datetime import UTC, date, datetime, time, timedelta

from app.crud.account_crud import delete_user_rows
from app.crud.demo_crud import seed_demo_data
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import USER_ID

TODAY = date(2026, 10, 15)


def _utc_now() -> datetime:
    """Naive UTC wall-clock time, the way the app stores task and session timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def _task(db, title, due_offset_days, archived=False, **fields) -> Task:
    task = Task(
        title=title,
        user_id=USER_ID,
        due_date=datetime.combine(TODAY + timedelta(days=due_offset_days), time.min),
        archived_at=datetime.now(UTC) if archived else None,
        **fields,
    )
    db.add(task)
    db.flush()
    return task


def _note_with_session(db, title, ended_at: datetime, minutes: int, archived=False) -> Note:
    note = Note(title=title, user_id=USER_ID, archived_at=datetime.now(UTC) if archived else None)
    db.add(note)
    db.flush()
    db.add(
        NoteSession(
            note_id=note.id,
            user_id=USER_ID,
            started_at=ended_at - timedelta(minutes=minutes),
            ended_at=ended_at,
        )
    )
    db.flush()
    return note


def test_debrief_excludes_archived_tasks(client, db):
    _task(db, "Overdue", -2)
    _task(db, "Due today", 0)
    _task(db, "Upcoming", 1, priority=1)
    _task(db, "Archived overdue", -2, archived=True)
    _task(db, "Archived due today", 0, archived=True, estimated_time=3)
    _task(db, "Archived upcoming", 1, archived=True, priority=2)
    db.commit()

    resp = client.get("/daily-debrief", params={"local_date": TODAY.isoformat()})
    assert resp.status_code == 200, resp.text
    report = resp.json()

    assert [t["title"] for t in report["overdue_tasks"]] == ["Overdue"]
    assert [t["title"] for t in report["due_today_tasks"]] == ["Due today"]
    assert [t["title"] for t in report["focus_next"]] == ["Upcoming"]
    assert report["workload"]["committed_minutes"] == 0


def test_debrief_completed_today_excludes_archived_tasks(client, db):
    now = _utc_now()
    _task(db, "Done today", 0, completed=True, completed_date=now)
    _task(db, "Archived done today", 0, archived=True, completed=True, completed_date=now)
    db.commit()

    local_today = now.date().isoformat()
    report = client.get("/daily-debrief", params={"local_date": local_today, "local_time": now.strftime("%H:%M")})
    assert [t["title"] for t in report.json()["completed_today_tasks"]] == ["Done today"]


def test_debrief_notes_worked_today_excludes_archived_notes(client, db):
    now = _utc_now()
    _note_with_session(db, "Active", now - timedelta(minutes=5), 30)
    _note_with_session(db, "Archived", now - timedelta(minutes=5), 45, archived=True)
    db.commit()

    for params in (
        {"local_date": now.date().isoformat(), "local_time": now.strftime("%H:%M")},
        {"local_date": now.date().isoformat()},
    ):
        report = client.get("/daily-debrief", params=params).json()
        assert [n["title"] for n in report["notes_worked_today"]] == ["Active"]


def test_note_time_totals_exclude_archived_notes(client, db):
    ended = _utc_now() - timedelta(hours=1)
    active = _note_with_session(db, "Active", ended, 30)
    _note_with_session(db, "Archived", ended, 45, archived=True)
    db.commit()
    expected = [{"note_id": active.id, "total_seconds": 30 * 60}]

    assert client.get("/note-session/totals").json() == expected

    day = ended.date()
    range_params = {
        "start_date": (day - timedelta(days=1)).isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
    }
    assert client.get("/note-session/totals-range", params=range_params).json() == expected
    assert client.get("/note-session/totals-range", params={**range_params, "utc_offset_minutes": 0}).json() == expected


def test_restored_note_counts_toward_totals_again(client, db):
    note = _note_with_session(db, "Was archived", _utc_now() - timedelta(hours=1), 20, archived=True)
    db.commit()
    assert client.get("/note-session/totals").json() == []

    client.patch(f"/unarchive-note/{note.id}")
    assert client.get("/note-session/totals").json() == [{"note_id": note.id, "total_seconds": 20 * 60}]


def test_debrief_habit_status_excludes_archived_habits(client, db):
    db.add_all([
        Habit(title="Active", user_id=USER_ID),
        Habit(title="Archived", user_id=USER_ID, archived_at=datetime.now(UTC)),
    ])
    db.commit()

    report = client.get("/daily-debrief", params={"local_date": TODAY.isoformat()}).json()
    assert [h["title"] for h in report["habit_status"]] == ["Active"]


def test_verify_streaks_skips_archived_habits(client, db):
    active = Habit(title="Active", user_id=USER_ID, current_streak=3, max_streak=3)
    archived = Habit(title="Archived", user_id=USER_ID, current_streak=4, max_streak=4, archived_at=datetime.now(UTC))
    db.add_all([active, archived])
    db.commit()

    assert client.post("/verify-streaks", params={"local_date": TODAY.isoformat()}).json() == {"reset_count": 1}
    db.refresh(active)
    db.refresh(archived)
    assert active.current_streak == 0
    assert archived.current_streak == 4


def test_archived_habit_keeps_logged_today(client):
    habit = client.post("/create-habit", json={"title": "Stretch"}).json()
    params = {"local_date": TODAY.isoformat()}
    client.patch(f"/toggle-habit/{habit['id']}", params=params)

    archived = client.patch(f"/archive-habit/{habit['id']}", params=params).json()
    assert archived["logged_today"] is True
    [listed] = client.get("/get-habits", params={**params, "archived": "true"}).json()
    assert listed["logged_today"] is True
    assert listed["current_streak"] == 1


def test_account_deletion_and_demo_seed_handle_archived_rows(client, db):
    archived_at = datetime.now(UTC)
    parent = _task(db, "Archived parent", 0, archived=True)
    _task(db, "Archived child", 0, archived=True, parent_task_id=parent.id)
    _note_with_session(db, "Archived note", _utc_now(), 10, archived=True)
    db.add(Tag(name="Archived tag", user_id=USER_ID, archived_at=archived_at))
    db.commit()

    seed_demo_data(db, USER_ID, local_date=TODAY.isoformat())
    assert client.get("/get-tasks", params={"archived": "true"}).json() == []
    assert client.get("/get-notes", params={"archived": "true"}).json() == []
    assert client.get("/get-tags", params={"archived": "true"}).json() == []
    assert client.get("/get-tasks").json()

    delete_user_rows(db, USER_ID)
    db.commit()
    for model in (Task, Note, Tag):
        assert db.query(model).filter(model.user_id == USER_ID).count() == 0
