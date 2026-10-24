"""Tasks added from Canvas: the link fields, the duplicate check and the linked-task sync."""
# The Canvas fixtures are shared with test_canvas_read.py by importing them.
# ruff: noqa: F811

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.core import canvas_cache
from app.crud import canvas_link_crud, canvas_sample
from app.crud.canvas_crud import get_connection
from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID, USER_ID
from tests.test_canvas_read import (  # noqa: F401 (fixtures)
    BASE_URL,
    PAST,
    SOON,
    TOKEN,
    canvas,
    connected,
    demo_client,
    fresh_state,
    sleeps,
)

LA = ZoneInfo("America/Los_Angeles")
SYNC = "/canvas/linked-tasks/sync"


def _link(assignment_id, course_id=11):
    return {
        "canvas_assignment_id": assignment_id,
        "canvas_html_url": f"{BASE_URL}/courses/{course_id}/assignments/{assignment_id}",
    }


def _create(client, **fields):
    resp = client.post("/create-task", json={"title": "Task", **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _due(iso: str, zone=LA) -> tuple[str, str]:
    """An ISO due_at as the (due_date, due_time) a task shows in zone, to the minute."""
    local = datetime.fromisoformat(iso).astimezone(zone)
    return local.date().isoformat(), local.time().replace(second=0).isoformat()


def _sync(client, **params):
    resp = client.post(SYNC, params={"tz": "America/Los_Angeles", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_task(body) -> dict[int, dict]:
    return {r["task_id"]: r for r in body["results"]}


def _task(db, task_id) -> Task:
    db.expire_all()
    return db.get(Task, task_id)


# ── The link fields ──────────────────────────────────────────────────────────

def test_tasks_carry_the_canvas_link(client):
    big = 10_000_000_000_301  # a sharded Canvas id, past 32 bits
    task = _create(client, canvas_assignment_id=big, canvas_html_url=f"{BASE_URL}/courses/11/assignments/301")
    assert (task["canvas_assignment_id"], task["canvas_html_url"]) == (big, f"{BASE_URL}/courses/11/assignments/301")
    plain = _create(client)
    assert (plain["canvas_assignment_id"], plain["canvas_html_url"]) == (None, None)


def test_only_https_links_are_stored(client):
    for url in ("javascript:alert(1)", "http://sdsu.instructure.com/courses/1", "https://u:p@x.example.com/",
                "https://x.example.com:8443/", "/courses/11/assignments/1"):
        resp = client.post("/create-task", json={"title": "Task", "canvas_html_url": url})
        assert resp.status_code == 422, url
    assert client.post("/create-task", json={"title": "Task", "canvas_assignment_id": 0}).status_code == 422


def test_saving_a_task_without_the_link_fields_keeps_its_link(client):
    task = _create(client, **_link(301))
    resp = client.put(f"/update-task/{task['id']}", json={"title": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["canvas_assignment_id"] == 301
    resp = client.put(f"/update-task/{task['id']}", json={"title": "Renamed", "canvas_assignment_id": None,
                                                            "canvas_html_url": None})
    assert (resp.json()["canvas_assignment_id"], resp.json()["canvas_html_url"]) == (None, None)


# ── Duplicates ───────────────────────────────────────────────────────────────

def test_a_second_open_task_for_an_assignment_is_a_409_with_the_first(client):
    first = _create(client, **_link(301))
    resp = client.post("/create-task", json={"title": "Again", **_link(301)})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "canvas_task_exists",
        "message": "An open task already follows this Canvas assignment.",
        "task_id": first["id"],
    }


def test_a_closed_task_does_not_block_a_new_one(client, db):
    completed = _create(client, title="Done", **_link(301))
    client.patch(f"/update-task-status/{completed['id']}")
    archived = _create(client, title="Archived", **_link(302))
    client.patch(f"/archive-task/{archived['id']}")
    deleted = _create(client, title="Deleted", **_link(303))
    client.delete(f"/del-task/{deleted['id']}")
    db.add(Task(title="Someone else's", user_id=OTHER_USER_ID, created_date=datetime.now(), canvas_assignment_id=304))
    db.commit()

    for assignment_id in (301, 302, 303, 304):
        _create(client, **_link(assignment_id))
    # A completed task is not an open one either.
    _create(client, completed=True, **_link(301))


def test_linking_an_existing_task_to_a_followed_assignment_is_a_409(client):
    first = _create(client, **_link(301))
    other = _create(client, title="Other")
    resp = client.put(f"/update-task/{other['id']}", json={"title": "Other", **_link(301)})
    assert (resp.status_code, resp.json()["detail"]["task_id"]) == (409, first["id"])
    # Re-saving the task that already follows it is fine.
    assert client.put(f"/update-task/{first['id']}", json={"title": "Edited", **_link(301)}).status_code == 200


def test_reopening_a_task_through_update_while_another_follows_its_assignment_is_a_409(client):
    old = _create(client, completed=True, **_link(301))
    new = _create(client, **_link(301))
    resp = client.put(f"/update-task/{old['id']}", json={"title": "Task", "completed": False})
    assert (resp.status_code, resp.json()["detail"]["task_id"]) == (409, new["id"])


def test_a_task_reopened_toggled_unarchived_or_restored_over_a_taken_assignment_comes_back_unlinked(client):
    url = _link(301)["canvas_html_url"]
    completed = _create(client, title="Completed", **_link(301))
    client.patch(f"/update-task-status/{completed['id']}")
    archived = _create(client, title="Archived", **_link(301))
    client.patch(f"/archive-task/{archived['id']}")
    deleted = _create(client, title="Deleted", **_link(301))
    client.delete(f"/del-task/{deleted['id']}")
    current = _create(client, title="Current", **_link(301))

    for resp in (
        client.patch(f"/update-task-status/{completed['id']}"),
        client.patch(f"/unarchive-task/{archived['id']}"),
        client.patch(f"/restore-task/{deleted['id']}"),
    ):
        assert resp.status_code == 200
        assert (resp.json()["canvas_assignment_id"], resp.json()["canvas_html_url"]) == (None, url)
    tasks = {t["id"]: t for t in client.get("/get-tasks").json()}
    assert tasks[current["id"]]["canvas_assignment_id"] == 301


def test_a_task_reopened_with_its_assignment_free_keeps_its_link(client):
    task = _create(client, **_link(301))
    client.patch(f"/update-task-status/{task['id']}")
    assert client.patch(f"/update-task-status/{task['id']}").json()["canvas_assignment_id"] == 301
    client.patch(f"/archive-task/{task['id']}")
    assert client.patch(f"/unarchive-task/{task['id']}").json()["canvas_assignment_id"] == 301


# ── Sync ─────────────────────────────────────────────────────────────────────

def test_sync_completes_tasks_whose_assignment_is_done(connected, db, canvas):
    graded = _create(connected, title="Problem Set 5", **_link(301))
    open_ = _create(connected, title="Quiz 3", **_link(302))
    body = _sync(connected)
    results = _by_task(body)
    assert results[graded["id"]]["submitted"] is True and results[graded["id"]]["synced"] is True
    assert (results[graded["id"]]["done"], results[graded["id"]]["done_reason"]) == (True, "graded")
    assert results[graded["id"]]["completed"] is True
    assert (results[open_["id"]]["submitted"], results[open_["id"]]["done"]) == (False, False)
    assert results[open_["id"]]["completed"] is False
    assert body["sample"] is False and body["busy"] is False and body["stale"] is False
    row = _task(db, graded["id"])
    assert row.completed is True and row.completed_date is not None
    assert row.canvas_completed_at is not None and row.completed_by_canvas is True
    assert _task(db, open_["id"]).completed is False
    assert canvas.paths() == ["/api/v1/courses/11/assignments", "/api/v1/planner/overrides"]


def _assignment(canvas, assignment_id) -> dict:
    return next(a for a in canvas.lists["/api/v1/courses/11/assignments"][0] if a["id"] == assignment_id)


def _override(plannable_id, plannable_type="assignment", **fields):
    """A planner override checking off the plannable; fields replace any of its values."""
    return {
        "id": 900 + plannable_id, "plannable_type": plannable_type, "plannable_id": plannable_id,
        "assignment_id": plannable_id, "workflow_state": "active", "marked_complete": True, "dismissed": False,
        **fields,
    }


def test_each_done_reason_completes_the_task(connected, db, canvas):
    _assignment(canvas, 302)["submission"] = {"workflow_state": "submitted", "submitted_at": PAST}
    _assignment(canvas, 303)["submission"] = {"workflow_state": "unsubmitted", "excused": True}
    canvas.lists["/api/v1/courses/11/assignments"][0].append(
        {"id": 304, "name": "Reading", "due_at": SOON, "submission_types": ["none"],
         "submission": {"workflow_state": "unsubmitted"}},
    )
    canvas.lists["/api/v1/planner/overrides"] = [[_override(304)]]
    tasks = {reason: _create(connected, **_link(assignment_id))
             for reason, assignment_id in (("graded", 301), ("submitted", 302), ("excused", 303),
                                           ("marked_complete", 304))}
    results = _by_task(_sync(connected))
    for reason, task in tasks.items():
        result = results[task["id"]]
        assert (result["done"], result["done_reason"], result["completed"]) == (True, reason, True), reason
        assert _task(db, task["id"]).completed_by_canvas is True


def test_a_quiz_checked_off_in_the_planner_is_matched_by_its_assignment_id(connected, db, canvas):
    task = _create(connected, **_link(302))
    # A classic quiz's override is on the quiz (quiz_id 9) and names its assignment.
    canvas.lists["/api/v1/planner/overrides"] = [[_override(9, plannable_type="quiz", assignment_id=302)]]
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["done_reason"], result["completed"]) == ("marked_complete", True)


def test_an_assignment_override_without_assignment_id_matches_by_plannable(connected, canvas):
    task = _create(connected, **_link(302))
    canvas.lists["/api/v1/planner/overrides"] = [[_override(302, assignment_id=None)]]
    assert _by_task(_sync(connected))[task["id"]]["done_reason"] == "marked_complete"


def test_unchecked_deleted_or_other_overrides_are_not_done(connected, db, canvas):
    task = _create(connected, **_link(302))
    canvas.lists["/api/v1/planner/overrides"] = [[
        _override(302, marked_complete=False, dismissed=True),
        _override(302, workflow_state="deleted"),
        _override(302, plannable_type="planner_note", assignment_id=None),
    ]]
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["done"], result["done_reason"], result["completed"]) == (False, None, False)
    assert _task(db, task["id"]).completed is False


def test_with_auto_complete_off_done_is_reported_but_the_task_stays_open(connected, db, canvas):
    connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    response = connected.patch("/canvas/connection", json={"auto_complete": False})
    assert response.status_code == 200
    # Each setting is left alone when omitted.
    assert response.json()["auto_complete"] is False and response.json()["hidden_course_ids"] == [12]
    task = _create(connected, **_link(301))
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["done"], result["done_reason"], result["completed"]) == (True, "graded", False)
    row = _task(db, task["id"])
    assert row.completed is False and row.canvas_completed_at is None

    assert connected.patch("/canvas/connection", json={"auto_complete": True}).json()["auto_complete"] is True
    assert _by_task(_sync(connected))[task["id"]]["completed"] is True


def test_auto_complete_is_on_by_default(connected):
    body = connected.get("/canvas/connection").json()
    assert body["auto_complete"] is True and body["capabilities"]["done_status"] is True


def test_a_completed_task_is_not_synced_again(connected, db, canvas):
    task = _create(connected, **_link(301))
    assert _by_task(_sync(connected))[task["id"]]["completed"] is True
    stamp = _task(db, task["id"]).canvas_completed_at
    canvas_cache.clear()
    assert _sync(connected)["results"] == []
    assert _task(db, task["id"]).canvas_completed_at == stamp


def test_a_task_the_user_reopens_is_not_completed_again(connected, db, canvas):
    task = _create(connected, **_link(301))
    _sync(connected)
    listed = next(t for t in connected.get("/get-tasks").json() if t["id"] == task["id"])
    assert listed["completed_by_canvas"] is True and listed["canvas_completed_at"] is not None

    reopened = connected.patch(f"/update-task-status/{task['id']}").json()
    assert reopened["completed"] is False and reopened["completed_by_canvas"] is False
    canvas_cache.clear()
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["done"], result["completed"]) == (True, False)
    assert _task(db, task["id"]).completed is False

    # Completing it again by hand isn't Canvas's doing.
    completed = connected.patch(f"/update-task-status/{task['id']}").json()
    assert completed["completed"] is True and completed["completed_by_canvas"] is False


def test_a_task_the_user_completed_is_left_alone(connected, db, canvas):
    task = _create(connected, **_link(301))
    connected.patch(f"/update-task-status/{task['id']}")
    completed_date = _task(db, task["id"]).completed_date
    assert _sync(connected)["results"] == []
    row = _task(db, task["id"])
    assert row.completed_date == completed_date and row.canvas_completed_at is None
    assert row.completed_by_canvas is False


def test_canvas_unmarking_an_item_leaves_its_completed_task_alone(connected, db, canvas):
    task = _create(connected, **_link(302))
    canvas.lists["/api/v1/planner/overrides"] = [[_override(302)]]
    assert _by_task(_sync(connected))[task["id"]]["completed"] is True
    canvas.lists["/api/v1/planner/overrides"] = [[_override(302, marked_complete=False)]]
    canvas_cache.clear()
    _sync(connected)
    row = _task(db, task["id"])
    assert row.completed is True and row.completed_by_canvas is True


def test_relinking_a_task_clears_canvas_completed_at(connected, db, canvas):
    task = _create(connected, **_link(301))
    _sync(connected)
    connected.patch(f"/update-task-status/{task['id']}")
    assert _task(db, task["id"]).canvas_completed_at is not None
    resp = connected.put(f"/update-task/{task['id']}", json={"title": "Task", **_link(302)})
    assert resp.status_code == 200
    assert _task(db, task["id"]).canvas_completed_at is None


def test_overrides_canvas_cannot_serve_leave_done_to_submissions(connected, db, canvas):
    graded = _create(connected, **_link(301))
    checked = _create(connected, **_link(302))
    canvas.lists["/api/v1/planner/overrides"] = [[_override(302)]]
    canvas.respond = lambda request: httpx.Response(503) if request.url.path == "/api/v1/planner/overrides" else None
    body = _sync(connected)
    results = _by_task(body)
    assert body["stale"] is True
    assert results[graded["id"]]["completed"] is True
    assert (results[checked["id"]]["synced"], results[checked["id"]]["done"]) == (True, False)


def test_sync_moves_a_changed_due_date_in_the_callers_time_zone(connected, db, canvas):
    task = _create(connected, **_link(302), due_date="2026-01-01", due_time="09:00:00")
    result = _by_task(_sync(connected))[task["id"]]
    assert result["due_changed"] is True
    assert datetime.fromisoformat(result["due_at"]) == datetime.fromisoformat(SOON)
    row = _task(db, task["id"])
    assert (row.due_date.date().isoformat(), row.due_time.isoformat()) == _due(SOON)

    # The same due date again is unchanged.
    assert _by_task(_sync(connected))[task["id"]]["due_changed"] is False


def test_sync_leaves_a_matching_due_date_alone(connected, db, canvas):
    due_date, due_time = _due(PAST)
    task = _create(connected, **_link(301), due_date=due_date, due_time=due_time)
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["synced"], result["due_changed"]) == (True, False)
    row = _task(db, task["id"])
    assert (row.due_date.date().isoformat(), row.due_time.isoformat()) == (due_date, due_time)


def test_sync_clears_the_due_date_when_canvas_drops_it(connected, db, canvas):
    task = _create(connected, **_link(302), due_date="2026-01-01", due_time="09:00:00")
    _sync(connected)
    canvas.lists["/api/v1/courses/11/assignments"][0][1]["due_at"] = None
    canvas_cache.clear()
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["due_changed"], result["due_at"]) == (True, None)
    row = _task(db, task["id"])
    assert (row.due_date, row.due_time) == (None, None)


def test_the_first_sync_keeps_a_due_date_changed_after_adding(connected, db, canvas):
    task = _create(connected, **_link(302), canvas_due_at=SOON, due_date="2026-01-01", due_time="09:00:00")
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["synced"], result["due_changed"]) == (True, False)
    row = _task(db, task["id"])
    assert (row.due_date.date().isoformat(), row.due_time.isoformat()) == ("2026-01-01", "09:00:00")


def test_relinking_stores_the_new_assignments_due_date(connected, db, canvas):
    task = _create(connected, **_link(301), canvas_due_at=PAST)
    resp = connected.put(f"/update-task/{task['id']}", json={"title": "Task", **_link(302), "canvas_due_at": SOON,
                                                              "due_date": "2026-01-01", "due_time": "09:00:00"})
    assert resp.status_code == 200
    assert datetime.fromisoformat(SOON) == _task(db, task["id"]).canvas_due_at
    assert _by_task(_sync(connected))[task["id"]]["due_changed"] is False
    assert _task(db, task["id"]).due_date.date().isoformat() == "2026-01-01"


def test_a_canvas_due_at_without_a_time_zone_is_refused(client):
    resp = client.post("/create-task", json={"title": "Task", **_link(301), "canvas_due_at": "2026-10-23T23:59:00"})
    assert resp.status_code == 422


def test_sync_keeps_a_due_date_the_user_changed(connected, db, canvas):
    task = _create(connected, **_link(302))
    _sync(connected)
    resp = connected.put(f"/update-task/{task['id']}", json={"title": "Task", "due_date": "2026-01-01",
                                                              "due_time": "09:00:00"})
    assert resp.status_code == 200
    canvas_cache.clear()
    result = _by_task(_sync(connected))[task["id"]]
    assert (result["synced"], result["due_changed"]) == (True, False)
    row = _task(db, task["id"])
    assert (row.due_date.date().isoformat(), row.due_time.isoformat()) == ("2026-01-01", "09:00:00")


def test_sync_follows_canvas_moving_the_due_date_after_a_user_edit(connected, db, canvas):
    task = _create(connected, **_link(302))
    _sync(connected)
    connected.put(f"/update-task/{task['id']}", json={"title": "Task", "due_date": "2026-01-01",
                                                       "due_time": "09:00:00"})
    canvas.lists["/api/v1/courses/11/assignments"][0][1]["due_at"] = PAST
    canvas_cache.clear()
    result = _by_task(_sync(connected))[task["id"]]
    assert result["due_changed"] is True
    row = _task(db, task["id"])
    assert (row.due_date.date().isoformat(), row.due_time.isoformat()) == _due(PAST)


def test_sync_uses_the_panels_cache(connected, canvas):
    _create(connected, **_link(301))
    assert connected.get("/canvas/courses/11/assignments").status_code == 200
    canvas.requests.clear()
    _sync(connected)
    # Only the planner overrides, which the panel doesn't read, come from Canvas.
    assert canvas.paths() == ["/api/v1/planner/overrides"]


def test_tasks_whose_assignment_cannot_be_read_are_left_alone(connected, db, canvas):
    gone = _create(connected, title="Deleted on Canvas", **_link(399), due_date="2026-01-01")
    no_course = _create(connected, title="No course", canvas_assignment_id=302,
                        canvas_html_url=f"{BASE_URL}/calendar", due_date="2026-01-01")
    other_school = _create(connected, title="Other school", canvas_assignment_id=303,
                           canvas_html_url="https://other.instructure.com/courses/11/assignments/303")
    dropped = _create(connected, title="Dropped course", **_link(5, course_id=99))
    results = _by_task(_sync(connected))
    for task in (gone, no_course, other_school, dropped):
        assert results[task["id"]] == {
            "task_id": task["id"], "synced": False, "submitted": False, "done": False, "done_reason": None,
            "due_at": None, "due_changed": False, "completed": False,
        }
    assert _task(db, gone["id"]).due_date.date() == date(2026, 1, 1)
    # The dropped course's 404 doesn't fail the sync or the connection.
    assert sorted(canvas.paths()) == [
        "/api/v1/courses/11/assignments", "/api/v1/courses/99/assignments", "/api/v1/planner/overrides",
    ]
    assert get_connection(db, USER_ID).status == "active"


def test_sync_reads_at_most_the_course_cap(connected, canvas, monkeypatch):
    monkeypatch.setattr(canvas_link_crud, "MAX_SYNC_COURSES", 1)
    first = _create(connected, **_link(301))
    second = _create(connected, **_link(1, course_id=12))
    results = _by_task(_sync(connected))
    assert results[first["id"]]["synced"] is True and results[second["id"]]["synced"] is False
    assert canvas.paths() == ["/api/v1/courses/11/assignments", "/api/v1/planner/overrides"]


def test_only_open_tasks_are_synced(connected, canvas):
    done = _create(connected, completed=True, **_link(301))
    archived = _create(connected, **_link(302))
    connected.patch(f"/archive-task/{archived['id']}")
    _create(connected, title="Not from Canvas")
    assert _sync(connected)["results"] == []
    assert canvas.requests == []
    assert done["completed"] is True


def test_sync_is_scoped_to_the_callers_tasks(connected, db, canvas):
    theirs = Task(title="Theirs", user_id=OTHER_USER_ID, created_date=datetime.now(), canvas_assignment_id=302,
                  canvas_html_url=f"{BASE_URL}/courses/11/assignments/302")
    db.add(theirs)
    db.commit()
    mine = _create(connected, **_link(302))
    assert [r["task_id"] for r in _sync(connected)["results"]] == [mine["id"]]
    assert _task(db, theirs.id).due_date is None


def test_an_invalid_token_needs_reconnecting(connected, db, canvas):
    task = _create(connected, **_link(302), due_date="2026-01-01")
    del canvas.valid[TOKEN]
    resp = connected.post(SYNC)
    assert (resp.status_code, resp.json()["detail"]) == (409, "canvas_token_invalid")
    assert get_connection(db, USER_ID).status == "invalid"
    assert _task(db, task["id"]).due_date.date() == date(2026, 1, 1)

    canvas.requests.clear()
    assert connected.post(SYNC).json()["detail"] == "canvas_token_invalid"
    assert canvas.requests == []


def test_unreachable_canvas_with_nothing_cached_is_unavailable(connected, canvas):
    _create(connected, **_link(302))
    canvas.respond = lambda request: httpx.Response(503)
    resp = connected.post(SYNC)
    assert (resp.status_code, resp.json()["detail"]) == (502, "canvas_unavailable")


def test_canvas_going_down_partway_returns_what_was_read(connected, canvas):
    first = _create(connected, **_link(301))
    second = _create(connected, **_link(1, course_id=12))
    canvas.respond = lambda request: httpx.Response(503) if "/courses/12/" in request.url.path else None
    body = _sync(connected)
    results = _by_task(body)
    assert results[first["id"]]["synced"] is True and results[second["id"]]["synced"] is False
    assert body["stale"] is True


def test_sync_without_a_connection_is_not_found(client, canvas):
    assert client.post(SYNC).json()["results"] == []  # nothing to sync, so no connection is needed
    _create(client, **_link(301))
    resp = client.post(SYNC)
    assert (resp.status_code, resp.json()["detail"]) == (404, "canvas_not_connected")


def test_an_unknown_time_zone_is_refused(connected):
    assert connected.post(SYNC, params={"tz": "Mars/Olympus"}).status_code == 422


# ── Demo sandboxes ───────────────────────────────────────────────────────────

def test_demo_sandboxes_sync_against_sample_data_without_calling_canvas(demo_client, db, canvas):
    submitted = _create(demo_client, title="Homework 7", canvas_assignment_id=5202)
    upcoming = _create(demo_client, title="Problem Set 6", canvas_assignment_id=5101, due_date="2026-01-01")
    unknown = _create(demo_client, title="Not a sample", canvas_assignment_id=1)

    first = _sync(demo_client)
    assert first["sample"] is True
    results = _by_task(first)
    assert results[submitted["id"]]["submitted"] is True
    assert (results[submitted["id"]]["done_reason"], results[submitted["id"]]["completed"]) == ("submitted", True)
    assert _task(db, submitted["id"]).completed_by_canvas is True
    assert results[upcoming["id"]]["submitted"] is False and results[upcoming["id"]]["due_changed"] is True
    assert results[upcoming["id"]]["completed"] is False
    assert results[unknown["id"]]["synced"] is False

    today = datetime.now(LA).date()
    sample = canvas_sample.assignments_by_id(today, LA)[5101]
    row = _task(db, upcoming["id"])
    assert (row.due_date.date(), row.due_time) == (sample.due_at.date(), sample.due_at.time())
    assert sample.due_at.date() == today + timedelta(days=1)

    second = _sync(demo_client)
    assert _by_task(second)[upcoming["id"]]["due_changed"] is False
    assert canvas.requests == []
    assert get_connection(db, USER_ID) is None
