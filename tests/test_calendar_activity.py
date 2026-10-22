"""GET /get-calendar-activity: tasks, completions, note sessions and habit logs over a date range."""

from datetime import UTC, date, datetime, time, timedelta

from app.core.auth import UserInfo, get_current_user
from app.crud.demo_crud import seed_demo_data
from app.main import app
from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID, USER_ID

# A Monday-to-Sunday week.
WEEK_START = date(2026, 10, 12)
WEEK_END = date(2026, 10, 18)
WEEK = {"start_date": WEEK_START.isoformat(), "end_date": WEEK_END.isoformat()}
PACIFIC = "America/Los_Angeles"


def _task(db, title, due: date | None = None, user_id=USER_ID, archived=False, **fields) -> Task:
    task = Task(
        title=title,
        user_id=user_id,
        due_date=datetime.combine(due, time.min) if due else None,
        archived_at=datetime.now(UTC) if archived else None,
        **fields,
    )
    db.add(task)
    db.flush()
    return task


def _note(db, title, user_id=USER_ID, archived=False) -> Note:
    note = Note(title=title, user_id=user_id, archived_at=datetime.now(UTC) if archived else None)
    db.add(note)
    db.flush()
    return note


def _session(db, note, started_at: datetime, ended_at: datetime | None) -> NoteSession:
    session = NoteSession(note_id=note.id, user_id=note.user_id, started_at=started_at, ended_at=ended_at)
    db.add(session)
    db.flush()
    return session


def _habit(db, title, logged: list[date], user_id=USER_ID, **fields) -> Habit:
    habit = Habit(title=title, user_id=user_id, **fields)
    db.add(habit)
    db.flush()
    for day in logged:
        db.add(HabitLog(habit_id=habit.id, logged_date=day))
    db.flush()
    return habit


def _activity(client, **params):
    resp = client.get("/get-calendar-activity", params={**WEEK, "tz": "UTC", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_scheduled_tasks_cover_due_dates_in_range(client, db):
    tag = Tag(name="School", color="#8b5cf6", user_id=USER_ID)
    db.add(tag)
    _task(db, "Before", WEEK_START - timedelta(days=1))
    first = _task(db, "Monday", WEEK_START, due_time=time(9, 30), estimated_time=1.5)
    first.tags = [tag]
    _task(db, "Sunday done", WEEK_END, completed=True)
    _task(db, "After", WEEK_END + timedelta(days=1))
    _task(db, "No due date")
    _task(db, "Archived", WEEK_START, archived=True)
    _task(db, "Someone else's", WEEK_START, user_id=OTHER_USER_ID)
    db.commit()

    scheduled = _activity(client)["scheduled_tasks"]
    assert [t["title"] for t in scheduled] == ["Monday", "Sunday done"]
    monday, sunday = scheduled
    assert monday["due_date"] == WEEK_START.isoformat()
    assert monday["due_time"] == "09:30:00"
    assert monday["estimated_time"] == 1.5
    assert monday["completed"] is False
    assert [t["name"] for t in monday["tags"]] == ["School"]
    assert sunday["completed"] is True


def test_scheduled_subtasks_name_their_parent(client, db):
    root = _task(db, "Course project")
    middle = _task(db, "Milestone", parent_task_id=root.id)
    _task(db, "Write tests", WEEK_START, parent_task_id=middle.id)
    _task(db, "Standalone", WEEK_START)
    db.commit()

    scheduled = {t["title"]: t for t in _activity(client)["scheduled_tasks"]}
    assert scheduled["Write tests"]["parent_task"] == {"id": middle.id, "title": "Milestone"}
    assert scheduled["Standalone"]["parent_task"] is None


def test_completed_tasks_are_bucketed_by_the_callers_local_days(client, db):
    # 03:00 UTC on Monday is still Sunday evening (20:00) in Pacific time.
    _task(db, "Late Sunday Pacific", completed=True, completed_date=datetime(2026, 10, 12, 3, 0))
    # Due long before the week, finished during it.
    _task(db, "Overdue, finished", WEEK_START - timedelta(days=30), completed=True,
          completed_date=datetime(2026, 10, 15, 18, 0))
    _task(db, "Completed before tracking", completed=True, completed_date=None)
    _task(db, "Archived", completed=True, completed_date=datetime(2026, 10, 15, 18, 0), archived=True)
    _task(db, "Someone else's", completed=True, completed_date=datetime(2026, 10, 15, 18, 0),
          user_id=OTHER_USER_ID)
    db.commit()

    utc_week = _activity(client)["completed_tasks"]
    assert [t["title"] for t in utc_week] == ["Late Sunday Pacific", "Overdue, finished"]
    assert utc_week[0]["completed_date"] == "2026-10-12T03:00:00Z"

    pacific_week = _activity(client, tz=PACIFIC)["completed_tasks"]
    assert [t["title"] for t in pacific_week] == ["Overdue, finished"]


def test_range_crossing_a_dst_change_resolves_each_end_in_its_own_offset(client, db):
    note = _note(db, "Year in review")
    # Pacific is UTC-8 (PST) on Dec 31 and Jan 1, though UTC-7 (PDT) mid-year.
    # 23:30 PST on Dec 31, 2025: the night before the year.
    _task(db, "New Year's Eve 2025", completed=True, completed_date=datetime(2026, 1, 1, 7, 30))
    _session(db, note, datetime(2026, 1, 1, 7, 0), datetime(2026, 1, 1, 7, 45))
    # 23:30 PST on Dec 31, 2026: the year's last hour.
    _task(db, "New Year's Eve 2026", completed=True, completed_date=datetime(2027, 1, 1, 7, 30))
    _session(db, note, datetime(2027, 1, 1, 7, 0), datetime(2027, 1, 1, 7, 45))
    # A week whose Sunday, Nov 1, 2026, falls back from PDT to PST.
    # 23:30 PST on Sunday, and 23:30 PDT the Sunday before.
    _task(db, "Fall-back Sunday night", completed=True, completed_date=datetime(2026, 11, 2, 7, 30))
    _task(db, "Sunday before", completed=True, completed_date=datetime(2026, 10, 26, 6, 30))
    db.commit()

    year = _activity(client, start_date="2026-01-01", end_date="2026-12-31", tz=PACIFIC)
    assert [t["title"] for t in year["completed_tasks"]] == [
        "Sunday before",
        "Fall-back Sunday night",
        "New Year's Eve 2026",
    ]
    assert [s["started_at"] for s in year["note_sessions"]] == ["2027-01-01T07:00:00Z"]

    week = _activity(client, start_date="2026-10-26", end_date="2026-11-01", tz=PACIFIC)
    assert [t["title"] for t in week["completed_tasks"]] == ["Fall-back Sunday night"]


def test_completing_a_task_records_when_and_uncompleting_clears_it(client, db):
    task = _task(db, "Toggle me", WEEK_START)
    db.commit()
    today = datetime.now(UTC).date()
    around_today = {"start_date": (today - timedelta(days=1)).isoformat(), "end_date": today.isoformat()}

    done = client.patch(f"/update-task-status/{task.id}").json()
    assert done["completed"] is True and done["completed_date"] is not None
    completed = _activity(client, **around_today)["completed_tasks"]
    assert [t["id"] for t in completed] == [task.id]

    undone = client.patch(f"/update-task-status/{task.id}").json()
    assert undone["completed"] is False and undone["completed_date"] is None
    assert _activity(client, **around_today)["completed_tasks"] == []


def test_note_sessions_overlapping_the_range(client, db):
    note = _note(db, "Binary search trees")
    archived = _note(db, "Archived note", archived=True)
    other = _note(db, "Someone else's", user_id=OTHER_USER_ID)
    monday = datetime.combine(WEEK_START, time.min)
    _session(db, note, monday + timedelta(hours=10), monday + timedelta(hours=10, minutes=45))
    # Starts Sunday night before the week and runs past midnight into it.
    _session(db, note, monday - timedelta(minutes=30), monday + timedelta(minutes=15))
    _session(db, note, monday - timedelta(hours=2), monday - timedelta(hours=1))
    _session(db, note, monday + timedelta(hours=12), None)
    # Zero-length: an orphan closed by the reaper.
    _session(db, note, monday + timedelta(hours=14), monday + timedelta(hours=14))
    _session(db, archived, monday + timedelta(hours=10), monday + timedelta(hours=11))
    _session(db, other, monday + timedelta(hours=10), monday + timedelta(hours=11))
    db.commit()

    sessions = _activity(client)["note_sessions"]
    assert [(s["note_title"], s["duration_seconds"]) for s in sessions] == [
        ("Binary search trees", 45 * 60),
        ("Binary search trees", 45 * 60),
    ]
    assert sessions[0]["started_at"] == "2026-10-11T23:30:00Z"
    assert sessions[1]["note_id"] == note.id
    assert sessions[1]["ended_at"] == "2026-10-12T10:45:00Z"


def test_habit_completions_in_range(client, db):
    meditation = _habit(
        db, "Meditation", [WEEK_START - timedelta(days=1), WEEK_START, WEEK_END], estimated_time=0.25
    )
    reading = _habit(db, "Reading", [WEEK_START, WEEK_END + timedelta(days=1)])
    _habit(db, "Someone else's", [WEEK_START], user_id=OTHER_USER_ID)
    db.commit()

    completions = _activity(client)["habit_completions"]
    assert [(c["habit_id"], c["habit_title"], c["date"]) for c in completions] == [
        (meditation.id, "Meditation", WEEK_START.isoformat()),
        (reading.id, "Reading", WEEK_START.isoformat()),
        (meditation.id, "Meditation", WEEK_END.isoformat()),
    ]
    assert completions[0]["estimated_time"] == 0.25


def test_trashed_and_archived_items_are_left_out_until_restored(client, db):
    tag = Tag(name="School", color="#8b5cf6", user_id=USER_ID)
    db.add(tag)
    task = _task(db, "Trashed", WEEK_START, completed=True, completed_date=datetime(2026, 10, 13, 12, 0))
    task.tags = [tag]
    note = _note(db, "Trashed note")
    _session(db, note, datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 13, 10, 0))
    habit = _habit(db, "Trashed habit", [WEEK_START])
    archived_habit = _habit(db, "Archived habit", [WEEK_START])
    db.commit()

    for path in (f"/del-task/{task.id}", f"/del-note/{note.id}", f"/del-habit/{habit.id}"):
        assert client.delete(path).status_code == 200
    assert client.patch(f"/archive-habit/{archived_habit.id}").status_code == 200
    activity = _activity(client)
    for key in ("scheduled_tasks", "completed_tasks", "note_sessions", "habit_completions"):
        assert activity[key] == [], key

    for path in (f"/restore-task/{task.id}", f"/restore-note/{note.id}", f"/restore-habit/{habit.id}"):
        assert client.patch(path).status_code == 200
    assert client.delete(f"/del-tag/{tag.id}").status_code == 200
    activity = _activity(client)
    assert [t["title"] for t in activity["scheduled_tasks"]] == ["Trashed"]
    assert activity["scheduled_tasks"][0]["tags"] == []
    assert [t["title"] for t in activity["completed_tasks"]] == ["Trashed"]
    assert [s["note_title"] for s in activity["note_sessions"]] == ["Trashed note"]
    assert [c["habit_title"] for c in activity["habit_completions"]] == ["Trashed habit"]


def test_other_users_see_none_of_it(client, db):
    _task(db, "Mine", WEEK_START, completed=True, completed_date=datetime(2026, 10, 13, 12, 0))
    note = _note(db, "Mine")
    _session(db, note, datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 13, 10, 0))
    _habit(db, "Mine", [WEEK_START])
    db.commit()
    keys = ("scheduled_tasks", "completed_tasks", "note_sessions", "habit_completions")
    assert all(_activity(client)[key] for key in keys)

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=OTHER_USER_ID, email="other@example.com", provider="email", is_anonymous=False
    )
    activity = _activity(client)
    for key in keys:
        assert activity[key] == [], key


def test_range_validation(client):
    def status(start, end, **params):
        return client.get(
            "/get-calendar-activity", params={"start_date": start, "end_date": end, "tz": "UTC", **params}
        ).status_code

    assert status("2026-10-18", "2026-10-12") == 400
    assert status("2024-01-01", "2024-12-31") == 200  # a leap year
    assert status("2024-01-01", "2025-01-01") == 400
    assert status("2026-10-12", "not-a-date") == 422
    assert status("2026-10-12", "2026-10-18", tz="Nowhere/Special") == 422
    assert status("2026-10-12", "2026-10-18", tz="../etc/passwd") == 422
    assert client.get("/get-calendar-activity", params=WEEK).status_code == 422


def test_demo_seed_completions_appear_in_activity(client, db):
    today = datetime.now(UTC).date()
    seed_demo_data(db, USER_ID, local_date=today.isoformat())

    activity = _activity(
        client, start_date=(today - timedelta(days=10)).isoformat(), end_date=today.isoformat()
    )
    seeded_done = {t.title for t in db.query(Task).filter(Task.user_id == USER_ID, Task.completed == True)}  # noqa: E712
    assert {t["title"] for t in activity["completed_tasks"]} == seeded_done
    assert activity["note_sessions"] and activity["habit_completions"]


def test_include_archived_adds_archived_items_but_never_trashed_ones(client, db):
    _task(db, "Active", WEEK_START)
    archived_task = _task(db, "Archived", WEEK_START, completed=True,
                          completed_date=datetime(2026, 10, 13, 12, 0), archived=True)
    trashed_task = _task(db, "Trashed", WEEK_START, completed=True, completed_date=datetime(2026, 10, 13, 12, 0))
    note = _note(db, "Active note")
    archived_note = _note(db, "Archived note", archived=True)
    trashed_note = _note(db, "Trashed note")
    for n in (note, archived_note, trashed_note):
        _session(db, n, datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 13, 10, 0))
    _habit(db, "Active habit", [WEEK_START])
    archived_habit = _habit(db, "Archived habit", [WEEK_START])
    trashed_habit = _habit(db, "Trashed habit", [WEEK_START])
    _task(db, "Someone else's archived", WEEK_START, user_id=OTHER_USER_ID, archived=True)
    db.commit()
    assert client.patch(f"/archive-habit/{archived_habit.id}").status_code == 200
    for path in (f"/del-task/{trashed_task.id}", f"/del-note/{trashed_note.id}", f"/del-habit/{trashed_habit.id}"):
        assert client.delete(path).status_code == 200

    def titles(activity):
        return {
            "scheduled": [t["title"] for t in activity["scheduled_tasks"]],
            "completed": [t["title"] for t in activity["completed_tasks"]],
            "sessions": [s["note_title"] for s in activity["note_sessions"]],
            "habits": [c["habit_title"] for c in activity["habit_completions"]],
        }

    default = titles(_activity(client))
    assert default == titles(_activity(client, include_archived="false"))
    assert default == {
        "scheduled": ["Active"],
        "completed": [],
        "sessions": ["Active note"],
        "habits": ["Active habit"],
    }

    assert titles(_activity(client, include_archived="true")) == {
        "scheduled": ["Active", "Archived"],
        "completed": ["Archived"],
        "sessions": ["Active note", "Archived note"],
        "habits": ["Active habit", "Archived habit"],
    }
    assert _activity(client, include_archived="true")["completed_tasks"][0]["id"] == archived_task.id

    # Archiving a task archives its subtasks, which then come along too.
    parent = _task(db, "Parent", WEEK_END)
    _task(db, "Subtask", WEEK_END, parent_task_id=parent.id)
    db.commit()
    assert client.patch(f"/archive-task/{parent.id}").status_code == 200
    scheduled = titles(_activity(client, include_archived="true"))["scheduled"]
    assert scheduled[-2:] == ["Parent", "Subtask"]
    assert titles(_activity(client))["scheduled"] == ["Active"]
