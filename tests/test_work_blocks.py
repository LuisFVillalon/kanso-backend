"""Work blocks: planned "work on it" times for tasks and habits, and habits' usual time (planned_time)."""

from datetime import date, time, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud.account_crud import delete_user_rows
from app.crud.work_block_crud import _place
from app.models.habit_model import Habit
from app.models.tag_model import Tag
from app.models.task_model import Task
from app.models.work_block_model import WorkBlock
from tests.conftest import OTHER_USER_ID, USER_ID

PACIFIC = "America/Los_Angeles"
EASTERN = "America/New_York"
PAST = date(2026, 1, 5)
FUTURE = date(2030, 3, 4)


def _task(db, title="Essay draft", user_id=USER_ID, tag=None, **fields) -> Task:
    task = Task(title=title, user_id=user_id, **fields)
    if tag is not None:
        task.tags = [tag]
    db.add(task)
    db.flush()
    return task


def _habit(db, title="Read 20 pages", user_id=USER_ID, **fields) -> Habit:
    habit = Habit(title=title, user_id=user_id, **fields)
    db.add(habit)
    db.flush()
    return habit


def _block(client, day=FUTURE, start="17:00", end="18:00", tz=PACIFIC, **owner):
    body = {**owner, "date": day.isoformat(), "start_time": start, "end_time": end, "time_zone": tz}
    response = client.post("/create-work-block", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _activity(client, start: date, end: date | None = None, tz=PACIFIC, **params):
    response = client.get(
        "/get-calendar-activity",
        params={"start_date": start.isoformat(), "end_date": (end or start).isoformat(), "tz": tz, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _block_ids(client, start: date, end: date | None = None, **params) -> list[int]:
    return [b["id"] for b in _activity(client, start, end, **params)["work_blocks"]]


# ── Create, update, delete ───────────────────────────────────────────────────

def test_create_returns_the_block_with_its_tasks_title_and_tags(client, db):
    tag = Tag(name="COMM 103", user_id=USER_ID, color="#336699")
    db.add(tag)
    task = _task(db, tag=tag)

    block = _block(client, task_id=task.id)

    assert [t["name"] for t in block.pop("tags")] == ["COMM 103"]
    assert block == {
        "id": block["id"],
        "task_id": task.id,
        "habit_id": None,
        "title": "Essay draft",
        "date": "2030-03-04",
        "start_time": "17:00",
        "end_time": "18:00",
        "time_zone": PACIFIC,
        "starts_at": "2030-03-05T01:00:00Z",
        "ends_at": "2030-03-05T02:00:00Z",
    }


def test_a_habit_block_takes_the_habits_title(client, db):
    habit = _habit(db)
    block = _block(client, habit_id=habit.id, start="11:30", end="12:00")
    assert (block["task_id"], block["habit_id"], block["title"]) == (None, habit.id, "Read 20 pages")


@pytest.mark.parametrize(
    "body",
    [
        {},  # Neither owner.
        {"task_id": 1, "habit_id": 1},  # Both.
        {"task_id": 1, "start_time": "17:00", "end_time": "17:00"},  # Ends when it starts.
        {"task_id": 1, "start_time": "5 PM"},
        {"task_id": 1, "start_time": "17:00:30"},
        {"task_id": 1, "time_zone": "Mars/Olympus"},
    ],
)
def test_create_rejects_an_unworkable_block(client, body):
    base = {"date": "2030-03-04", "start_time": "17:00", "end_time": "18:00", "time_zone": PACIFIC}
    assert client.post("/create-work-block", json={**base, **body}).status_code == 422


def test_a_block_ending_before_it_starts_runs_past_midnight(client, db):
    block = _block(client, task_id=_task(db).id, start="23:00", end="00:30")
    assert (block["starts_at"], block["ends_at"]) == ("2030-03-05T07:00:00Z", "2030-03-05T08:30:00Z")


def test_a_time_skipped_by_dst_moves_to_the_matching_instant_after(client, db):
    # 2030-03-10 is the spring-forward day in the US: 02:00-03:00 doesn't exist, so 02:00 is 03:00 PDT.
    block = _block(client, task_id=_task(db).id, day=date(2030, 3, 10), start="02:00", end="04:00")
    assert (block["starts_at"], block["ends_at"]) == ("2030-03-10T10:00:00Z", "2030-03-10T11:00:00Z")


def test_only_the_users_open_active_tasks_and_active_habits_can_be_planned(client, db):
    others = _task(db, user_id=OTHER_USER_ID)
    done = _task(db, completed=True)
    archived = _task(db, archived_at=FUTURE)
    deleted = _task(db, deleted_at=FUTURE)
    archived_habit = _habit(db, archived_at=FUTURE)
    others_habit = _habit(db, user_id=OTHER_USER_ID)
    base = {"date": "2030-03-04", "start_time": "17:00", "end_time": "18:00", "time_zone": PACIFIC}

    assert client.post("/create-work-block", json={**base, "task_id": others.id}).status_code == 404
    assert client.post("/create-work-block", json={**base, "task_id": deleted.id}).status_code == 404
    assert client.post("/create-work-block", json={**base, "task_id": 999_999}).status_code == 404
    assert client.post("/create-work-block", json={**base, "habit_id": others_habit.id}).status_code == 404
    assert client.post("/create-work-block", json={**base, "task_id": done.id}).status_code == 400
    assert client.post("/create-work-block", json={**base, "task_id": archived.id}).status_code == 400
    assert client.post("/create-work-block", json={**base, "habit_id": archived_habit.id}).status_code == 400


def test_update_moves_and_resizes_a_block(client, db):
    block = _block(client, task_id=_task(db).id)

    response = client.put(
        f"/update-work-block/{block['id']}", json={"date": "2030-03-06", "start_time": "09:15", "end_time": "10:45"}
    )

    assert response.status_code == 200, response.text
    moved = response.json()
    assert (moved["date"], moved["start_time"], moved["end_time"]) == ("2030-03-06", "09:15", "10:45")
    assert (moved["starts_at"], moved["ends_at"]) == ("2030-03-06T17:15:00Z", "2030-03-06T18:45:00Z")
    assert moved["time_zone"] == PACIFIC
    assert _block_ids(client, FUTURE) == []
    assert _block_ids(client, date(2030, 3, 6)) == [block["id"]]


def test_update_can_take_the_zone_the_new_times_are_in(client, db):
    block = _block(client, task_id=_task(db).id)
    moved = client.put(
        f"/update-work-block/{block['id']}",
        json={"date": "2030-03-04", "start_time": "20:00", "end_time": "21:00", "time_zone": EASTERN},
    ).json()
    assert (moved["time_zone"], moved["starts_at"]) == (EASTERN, "2030-03-05T01:00:00Z")


def test_update_rejects_a_block_ending_when_it_starts(client, db):
    block = _block(client, task_id=_task(db).id)
    response = client.put(
        f"/update-work-block/{block['id']}", json={"date": "2030-03-04", "start_time": "10:00", "end_time": "10:00"}
    )
    assert response.status_code == 422


def test_delete_removes_the_block_and_returns_it(client, db):
    block = _block(client, task_id=_task(db).id)

    response = client.delete(f"/delete-work-block/{block['id']}")

    assert response.status_code == 200
    assert response.json() == block
    assert _block_ids(client, FUTURE) == []
    assert client.delete(f"/delete-work-block/{block['id']}").status_code == 404


def test_blocks_belong_to_their_owner(client, db):
    task = _task(db, user_id=OTHER_USER_ID)
    theirs = WorkBlock(user_id=OTHER_USER_ID, task_id=task.id)
    _place(theirs, FUTURE, time(17), time(18), PACIFIC)
    db.add(theirs)
    db.flush()
    move = {"date": "2030-03-04", "start_time": "09:00", "end_time": "10:00"}

    assert client.put(f"/update-work-block/{theirs.id}", json=move).status_code == 404
    assert client.delete(f"/delete-work-block/{theirs.id}").status_code == 404
    assert _block_ids(client, FUTURE) == []


# ── Calendar activity and the daily debrief ──────────────────────────────────

def test_calendar_activity_lists_blocks_overlapping_the_range_in_start_order(client, db):
    task, habit = _task(db), _habit(db)
    late = _block(client, task_id=task.id, start="17:00", end="18:00")
    early = _block(client, habit_id=habit.id, start="08:00", end="08:30")
    _block(client, task_id=task.id, day=FUTURE + timedelta(days=2))
    overnight = _block(client, task_id=task.id, day=FUTURE - timedelta(days=1), start="23:30", end="00:30")

    assert _block_ids(client, FUTURE) == [overnight["id"], early["id"], late["id"]]


def test_calendar_activity_places_blocks_in_the_callers_time_zone(client, db):
    block = _block(client, task_id=_task(db).id, start="22:00", end="23:00")

    [shown] = _activity(client, FUTURE + timedelta(days=1), tz=EASTERN)["work_blocks"]

    assert shown["id"] == block["id"]
    assert (shown["date"], shown["start_time"], shown["end_time"]) == ("2030-03-05", "01:00", "02:00")
    assert shown["time_zone"] == PACIFIC
    assert (shown["starts_at"], shown["ends_at"]) == (block["starts_at"], block["ends_at"])


def test_blocks_hide_with_their_trashed_task_or_habit_and_come_back_on_restore(client, db):
    task, habit = _task(db), _habit(db)
    task_block = _block(client, task_id=task.id)
    habit_block = _block(client, habit_id=habit.id, start="08:00", end="08:30")

    client.delete(f"/del-task/{task.id}")
    client.delete(f"/del-habit/{habit.id}")
    assert _block_ids(client, FUTURE) == []
    assert client.delete(f"/delete-work-block/{task_block['id']}").status_code == 404

    client.patch(f"/restore-task/{task.id}")
    client.patch(f"/restore-habit/{habit.id}")
    assert _block_ids(client, FUTURE) == [habit_block["id"], task_block["id"]]


def test_archived_habits_blocks_show_only_with_include_archived(client, db):
    habit = _habit(db)
    block = _block(client, habit_id=habit.id)
    client.patch(f"/archive-habit/{habit.id}")

    assert _block_ids(client, FUTURE) == []
    assert _block_ids(client, FUTURE, include_archived=True) == [block["id"]]


def test_the_debrief_lists_todays_blocks_and_habits_usual_time(client, db):
    habit = _habit(db)
    client.put(f"/update-habit/{habit.id}", json={"title": habit.title, "planned_time": "13:00"})
    task = _task(db)
    today = _block(client, task_id=task.id, start="12:00", end="13:00")
    habit_today = _block(client, habit_id=habit.id, start="17:00", end="17:30")
    _block(client, task_id=task.id, day=FUTURE + timedelta(days=1))
    # Starts the day before, so it isn't one of today's.
    _block(client, task_id=task.id, day=FUTURE - timedelta(days=1), start="23:30", end="00:30")

    for params in ({"tz": PACIFIC}, {}):
        params = {"local_date": FUTURE.isoformat(), "local_time": "09:00", **params}
        report = client.get("/daily-debrief", params=params)
        assert report.status_code == 200, report.text
        body = report.json()
        assert [b["id"] for b in body["work_blocks"]] == [today["id"], habit_today["id"]]
        assert [(h["id"], h["planned_time"]) for h in body["habit_status"]] == [(habit.id, "13:00")]
        # Work blocks aren't events.
        assert body["events_today"] == []
        assert body["workload"]["event_minutes"] == 0


def test_blocks_are_not_events_in_reports_or_reminders(client, db):
    day = date.today() + timedelta(days=1)
    _block(client, task_id=_task(db).id, day=day, start="09:00", end="11:00")

    report = client.get(
        "/get-event-report", params={"start_date": day.isoformat(), "end_date": day.isoformat(), "tz": PACIFIC}
    ).json()
    assert (report["event_count"], report["total_minutes"]) == (0, 0)
    notifications = client.get("/get-notifications", params={"tz": PACIFIC}).json()
    assert not [n for n in notifications if n["type"] == "event_reminder"]
    assert _activity(client, day)["events"] == []


# ── Completing, archiving and deleting tasks ─────────────────────────────────

def _past_and_future(client, task):
    return _block(client, task_id=task.id, day=PAST), _block(client, task_id=task.id, day=FUTURE)


def _all_ids(client) -> set[int]:
    ids = set()
    for day in (PAST, FUTURE):
        ids.update(_block_ids(client, day, include_archived=True))
    return ids


def test_completing_a_task_deletes_its_future_blocks_and_keeps_past_ones(client, db):
    task = _task(db)
    past, _ = _past_and_future(client, task)

    client.patch(f"/update-task-status/{task.id}")

    assert _all_ids(client) == {past["id"]}
    # Reopening it doesn't bring them back.
    client.patch(f"/update-task-status/{task.id}")
    assert _all_ids(client) == {past["id"]}


def test_complete_all_drops_the_linked_tasks_future_blocks_too(client, db):
    parent = _task(db, title="Project")
    child = _task(db, title="Outline", parent_task_id=parent.id)
    untouched = _task(db, title="Other")
    child_past, _ = _past_and_future(client, child)
    _, untouched_future = _past_and_future(client, untouched)
    untouched_past = _block_ids(client, PAST)[-1]

    client.patch(f"/update-task-status/{parent.id}", params={"with_linked": True})

    assert _all_ids(client) == {child_past["id"], untouched_past, untouched_future["id"]}


def test_completing_a_task_by_saving_it_drops_its_future_blocks(client, db):
    task = _task(db)
    past, _ = _past_and_future(client, task)

    response = client.put(f"/update-task/{task.id}", json={"title": task.title, "completed": True})

    assert response.status_code == 200, response.text
    assert _all_ids(client) == {past["id"]}


def test_saving_an_open_task_keeps_its_blocks(client, db):
    task = _task(db)
    blocks = _past_and_future(client, task)
    client.put(f"/update-task/{task.id}", json={"title": "Renamed"})
    assert _all_ids(client) == {b["id"] for b in blocks}
    assert _activity(client, FUTURE)["work_blocks"][0]["title"] == "Renamed"


def test_archiving_a_task_deletes_its_future_blocks_and_keeps_past_ones(client, db):
    parent = _task(db, title="Project")
    child = _task(db, title="Outline", parent_task_id=parent.id)
    parent_past, _ = _past_and_future(client, parent)
    child_past, _ = _past_and_future(client, child)

    client.patch(f"/archive-task/{parent.id}")

    assert _block_ids(client, PAST) == []
    assert _all_ids(client) == {parent_past["id"], child_past["id"]}


def test_purging_a_task_or_habit_deletes_its_blocks(client, db):
    task, habit = _task(db), _habit(db)
    _past_and_future(client, task)
    _block(client, habit_id=habit.id)
    client.delete(f"/del-task/{task.id}")
    client.delete(f"/del-habit/{habit.id}")

    client.delete(f"/purge-task/{task.id}")
    client.delete(f"/purge-habit/{habit.id}")

    db.expire_all()
    assert db.query(WorkBlock).filter(WorkBlock.user_id == USER_ID).count() == 0


def test_a_block_needs_exactly_one_owner_in_the_database(db):
    task, habit = _task(db), _habit(db)
    for owners in ({}, {"task_id": task.id, "habit_id": habit.id}):
        block = WorkBlock(user_id=USER_ID, **owners)
        _place(block, FUTURE, time(9), time(10), PACIFIC)
        nested = db.begin_nested()
        db.add(block)
        with pytest.raises(IntegrityError):
            db.flush()
        nested.rollback()


def test_account_deletion_removes_blocks(client, db):
    _block(client, task_id=_task(db).id)
    delete_user_rows(db, USER_ID)
    assert db.query(WorkBlock).filter(WorkBlock.user_id == USER_ID).count() == 0


# ── Habits' usual time ───────────────────────────────────────────────────────

def test_habits_save_and_return_their_usual_time(client):
    created = client.post("/create-habit", json={"title": "Spanish practice", "planned_time": "09:30"})
    assert created.status_code == 200, created.text
    habit = created.json()
    assert habit["planned_time"] == "09:30"
    assert [h["planned_time"] for h in client.get("/get-habits").json()] == ["09:30"]

    # Left out of an update, it's kept; null clears it.
    kept = client.put(f"/update-habit/{habit['id']}", json={"title": "Spanish"}).json()
    assert kept["planned_time"] == "09:30"
    moved = client.put(f"/update-habit/{habit['id']}", json={"title": "Spanish", "planned_time": "16:00"}).json()
    assert moved["planned_time"] == "16:00"
    cleared = client.put(f"/update-habit/{habit['id']}", json={"title": "Spanish", "planned_time": None}).json()
    assert cleared["planned_time"] is None


def test_a_habit_without_a_usual_time_reads_null(client):
    assert client.post("/create-habit", json={"title": "Stretch"}).json()["planned_time"] is None


@pytest.mark.parametrize("value", ["9:30", "24:00", "09:61", "9am", "09:30:15"])
def test_a_usual_time_must_be_hh_mm(client, value):
    assert client.post("/create-habit", json={"title": "Stretch", "planned_time": value}).status_code == 422
