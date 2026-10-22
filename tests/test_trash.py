"""Deleting moves tasks, notes, tags and habits to the trash for 30 days."""

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.crud.account_crud import delete_user_rows
from app.crud.base import TRASH_RETENTION
from app.crud.demo_crud import seed_demo_data
from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.note_session_model import NoteSession
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID, USER_ID

# kind -> (create path, create payload, list path, model)
KINDS = {
    "task": ("/create-task", {"title": "Write report"}, "/get-tasks", Task),
    "note": ("/create-note", {"title": "Lecture notes"}, "/get-notes", Note),
    "tag": ("/create-tags", {"name": "School"}, "/get-tags", Tag),
    "habit": ("/create-habit", {"title": "Stretch"}, "/get-habits", Habit),
}
SCHOOL = {"name": "School", "color": "#8b5cf6"}


def _create(client, kind, **overrides):
    path, payload, *_ = KINDS[kind]
    resp = client.post(path, json={**payload, **overrides})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _delete(client, kind, row_id):
    resp = client.delete(f"/del-{kind}/{row_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


def _trash(client):
    resp = client.get("/get-trash")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(db, kind, user_id=USER_ID, deleted_ago: timedelta | None = None, **fields):
    """Insert a row directly, deleted `deleted_ago` before now when given."""
    model = KINDS[kind][3]
    defaults = {"name": f"Tag {datetime.now().timestamp()}"} if kind == "tag" else {"title": "Direct"}
    row = model(
        user_id=user_id,
        deleted_at=datetime.now(UTC) - deleted_ago if deleted_ago is not None else None,
        **{**defaults, **fields},
    )
    db.add(row)
    db.commit()
    return row


# ── Delete, list, restore ────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_delete_moves_the_row_to_the_trash(client, db, kind):
    list_path, model = KINDS[kind][2], KINDS[kind][3]
    kept = _create(client, kind, **({"name": "Kept"} if kind == "tag" else {}))
    row = _create(client, kind)

    deleted = _delete(client, kind, row["id"])
    assert deleted["deleted_at"] is not None

    assert _ids(client.get(list_path)) == {kept["id"]}
    assert _ids(client.get(list_path, params={"archived": "true"})) == set()
    assert db.get(model, row["id"]) is not None, "a delete must keep the row for the trash"

    [item] = _trash(client)
    assert item["kind"] == kind
    assert item["id"] == row["id"]
    assert item["title"] == (row["name"] if kind == "tag" else row["title"])
    deleted_at = datetime.fromisoformat(item["deleted_at"])
    assert datetime.fromisoformat(item["purge_at"]) == deleted_at + TRASH_RETENTION


@pytest.mark.parametrize("kind", KINDS)
def test_restore_brings_the_row_back(client, kind):
    list_path = KINDS[kind][2]
    row = _create(client, kind)
    _delete(client, kind, row["id"])

    resp = client.patch(f"/restore-{kind}/{row['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_at"] is None
    assert _ids(client.get(list_path)) == {row["id"]}
    assert _trash(client) == []


@pytest.mark.parametrize("kind", KINDS)
def test_deleted_rows_are_gone_from_every_lookup(client, kind):
    row = _create(client, kind)
    _delete(client, kind, row["id"])

    assert client.delete(f"/del-{kind}/{row['id']}").status_code == 404
    assert client.patch(f"/archive-{kind}/{row['id']}").status_code == 404
    assert client.patch(f"/unarchive-{kind}/{row['id']}").status_code == 404
    assert client.put(f"/update-{kind}/{row['id']}", json=KINDS[kind][1]).status_code == 404


def test_deleted_habits_and_notes_reject_their_own_actions(client):
    habit = _create(client, "habit")
    note = _create(client, "note")
    _delete(client, "habit", habit["id"])
    _delete(client, "note", note["id"])

    assert client.patch(f"/toggle-habit/{habit['id']}").status_code == 404
    assert client.patch(f"/toggle-habit-date/{habit['id']}", json={"date": "2026-01-01"}).status_code == 404
    assert client.get(f"/habit-history/{habit['id']}").status_code == 404
    assert client.post("/note-session/start", json={"note_id": note["id"]}).status_code == 404


def test_trash_lists_newest_first(client, db):
    old = _row(db, "note", deleted_ago=timedelta(days=3))
    newer = _row(db, "habit", deleted_ago=timedelta(days=1))
    task = _create(client, "task")
    _delete(client, "task", task["id"])

    assert [(i["kind"], i["id"]) for i in _trash(client)] == [
        ("task", task["id"]),
        ("habit", newer.id),
        ("note", old.id),
    ]


# ── Owner scoping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_trash_is_scoped_to_its_owner(client, db, kind):
    foreign = _row(db, kind, user_id=OTHER_USER_ID, deleted_ago=timedelta(days=1))

    assert _trash(client) == []
    assert client.patch(f"/restore-{kind}/{foreign.id}").status_code == 404
    assert client.delete(f"/purge-{kind}/{foreign.id}").status_code == 404
    db.refresh(foreign)
    assert foreign.deleted_at is not None


@pytest.mark.parametrize("kind", KINDS)
def test_restore_and_purge_404_outside_the_trash(client, db, kind):
    live = _create(client, kind)

    for resp in (
        client.patch(f"/restore-{kind}/{live['id']}"),
        client.delete(f"/purge-{kind}/{live['id']}"),
        client.patch(f"/restore-{kind}/999999"),
        client.delete(f"/purge-{kind}/999999"),
    ):
        assert resp.status_code == 404
    assert db.get(KINDS[kind][3], live["id"]) is not None


# ── Purging ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_purge_deletes_from_the_trash_for_good(client, db, kind):
    row = _create(client, kind)
    _delete(client, kind, row["id"])

    resp = client.delete(f"/purge-{kind}/{row['id']}")
    assert resp.status_code == 204, resp.text
    assert _trash(client) == []
    db.expire_all()
    assert db.get(KINDS[kind][3], row["id"]) is None
    assert client.patch(f"/restore-{kind}/{row['id']}").status_code == 404


@pytest.mark.parametrize("kind", KINDS)
def test_rows_past_retention_are_gone_and_purged(client, db, kind):
    model = KINDS[kind][3]
    expired = _row(db, kind, deleted_ago=TRASH_RETENTION + timedelta(minutes=1))
    recent = _row(db, kind, deleted_ago=TRASH_RETENTION - timedelta(hours=1))
    expired_id = expired.id

    assert client.patch(f"/restore-{kind}/{expired_id}").status_code == 404
    db.expire_all()
    assert db.get(model, expired_id) is None
    assert [i["id"] for i in _trash(client)] == [recent.id]
    assert db.get(model, recent.id) is not None


def test_one_users_delete_purges_every_users_expired_rows(client, db):
    expired = _row(db, "note", deleted_ago=TRASH_RETENTION + timedelta(days=1))
    foreign = _row(db, "note", user_id=OTHER_USER_ID, deleted_ago=TRASH_RETENTION + timedelta(days=1))
    foreign_recent = _row(db, "habit", user_id=OTHER_USER_ID, deleted_ago=timedelta(days=1))
    expired_id, foreign_id = expired.id, foreign.id

    _delete(client, "task", _create(client, "task")["id"])
    db.expire_all()
    assert db.get(Note, expired_id) is None
    assert db.get(Note, foreign_id) is None
    assert db.get(Habit, foreign_recent.id) is not None


@pytest.mark.parametrize("kind", KINDS)
def test_opening_the_trash_purges_other_users_expired_rows(client, db, kind):
    model = KINDS[kind][3]
    foreign_id = _row(db, kind, user_id=OTHER_USER_ID, deleted_ago=TRASH_RETENTION + timedelta(days=1)).id

    _trash(client)
    db.expire_all()
    assert db.get(model, foreign_id) is None


def test_startup_purges_expired_rows_for_users_who_never_return(db, monkeypatch):
    expired_id = _row(db, "habit", user_id=OTHER_USER_ID, deleted_ago=TRASH_RETENTION + timedelta(days=1)).id
    db.add(HabitLog(habit_id=expired_id, logged_date=date.today()))
    db.commit()
    recent_id = _row(db, "note", user_id=OTHER_USER_ID, deleted_ago=timedelta(days=1)).id
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(db))

    with TestClient(main.app):
        pass
    db.expire_all()
    assert db.get(Habit, expired_id) is None
    assert db.query(HabitLog).filter(HabitLog.habit_id == expired_id).count() == 0
    assert db.get(Note, recent_id) is not None


# ── Associations survive a round trip ────────────────────────────────────────


def test_restored_task_keeps_its_tags(client):
    task = _create(client, "task", tags=[SCHOOL])
    _delete(client, "task", task["id"])
    restored = client.patch(f"/restore-task/{task['id']}").json()
    assert [t["name"] for t in restored["tags"]] == ["School"]


def test_restored_habit_keeps_its_logs_and_streak(client, db):
    habit = _row(db, "habit", current_streak=2, max_streak=2)
    today = date.today()
    db.add_all([HabitLog(habit_id=habit.id, logged_date=today - timedelta(days=i)) for i in range(2)])
    db.commit()

    _delete(client, "habit", habit.id)
    restored = client.patch(f"/restore-habit/{habit.id}", params={"local_date": today.isoformat()}).json()
    assert restored["logged_today"] is True
    assert restored["current_streak"] == 2
    history = client.get(f"/habit-history/{habit.id}", params={"days": 2, "local_date": today.isoformat()}).json()
    assert [e["logged"] for e in history] == [True, True]


def test_restored_note_keeps_its_editing_time(client, db):
    note = _row(db, "note")
    ended = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db.add(NoteSession(note_id=note.id, user_id=USER_ID, started_at=ended - timedelta(minutes=20), ended_at=ended))
    db.commit()
    expected = [{"note_id": note.id, "total_seconds": 20 * 60}]

    _delete(client, "note", note.id)
    assert client.get("/note-session/totals").json() == []
    assert client.get(f"/note-session/total/{note.id}").json()["total_seconds"] == 0

    client.patch(f"/restore-note/{note.id}")
    assert client.get("/note-session/totals").json() == expected
    assert client.get(f"/note-session/total/{note.id}").json()["total_seconds"] == 20 * 60


# ── Tags ─────────────────────────────────────────────────────────────────────


def test_deleted_tag_is_hidden_on_items_until_restored(client):
    task = _create(client, "task", tags=[SCHOOL])
    note = _create(client, "note", tags=[SCHOOL])
    habit = _create(client, "habit", tags=[SCHOOL])
    tag_id = task["tags"][0]["id"]

    _delete(client, "tag", tag_id)
    for list_path in ("/get-tasks", "/get-notes", "/get-habits"):
        [row] = client.get(list_path).json()
        assert row["tags"] == []

    # Re-saving an item without the hidden tag must not drop its link.
    resp = client.put(f"/update-task/{task['id']}", json={"title": "Renamed", "tags": []})
    assert resp.status_code == 200, resp.text
    resp = client.put(f"/update-habit/{habit['id']}", json={"title": "Renamed", "tags": []})
    assert resp.status_code == 200, resp.text

    client.patch(f"/restore-tag/{tag_id}")
    for list_path, row in (("/get-tasks", task), ("/get-notes", note), ("/get-habits", habit)):
        [listed] = client.get(list_path).json()
        assert listed["id"] == row["id"]
        assert [t["id"] for t in listed["tags"]] == [tag_id], list_path


def test_attaching_a_deleted_tag_by_name_restores_it(client):
    tagged = _create(client, "task", title="Tagged", tags=[SCHOOL])
    tag_id = tagged["tags"][0]["id"]
    _delete(client, "tag", tag_id)

    row = _create(client, "note", tags=[SCHOOL])
    assert [t["id"] for t in row["tags"]] == [tag_id]
    assert _trash(client) == []
    [listed] = client.get("/get-tasks").json()
    assert [t["id"] for t in listed["tags"]] == [tag_id]


def test_naming_a_hidden_deleted_tag_on_its_own_item_restores_it(client):
    task = _create(client, "task", tags=[SCHOOL])
    tag_id = task["tags"][0]["id"]
    _delete(client, "tag", tag_id)

    resp = client.put(f"/update-task/{task['id']}", json={"title": "Again", "tags": [SCHOOL]})
    assert resp.status_code == 200, resp.text
    assert [t["id"] for t in resp.json()["tags"]] == [tag_id]
    assert _ids(client.get("/get-tags")) == {tag_id}


def test_tag_name_conflict_with_deleted_tag_mentions_recently_deleted(client):
    tag = _create(client, "tag")
    _delete(client, "tag", tag["id"])

    resp = client.post("/create-tags", json={"name": "School"})
    assert resp.status_code == 409
    assert "Recently Deleted" in resp.json()["detail"]


def test_expired_deleted_tag_frees_its_name(client, db):
    expired = _row(db, "tag", deleted_ago=TRASH_RETENTION + timedelta(days=1), name="School")
    expired_id = expired.id

    resp = client.post("/create-tags", json={"name": "School"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] != expired_id


def test_expired_deleted_tag_is_not_revived_by_attaching_its_name(client, db):
    task = _create(client, "task", tags=[SCHOOL])
    tag = db.get(Tag, task["tags"][0]["id"])
    tag.deleted_at = datetime.now(UTC) - TRASH_RETENTION - timedelta(days=1)
    db.commit()

    resp = client.put(f"/update-task/{task['id']}", json={"title": "Again", "tags": [SCHOOL]})
    assert resp.status_code == 200, resp.text
    [new_tag] = resp.json()["tags"]
    assert new_tag["id"] != tag.id
    db.expire_all()
    assert db.get(Tag, tag.id) is None


# ── Tasks and subtasks ───────────────────────────────────────────────────────


def test_deleting_a_task_takes_its_subtasks_along(client, db):
    parent = _create(client, "task", title="Parent")
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])
    grandchild = _create(client, "task", title="Grandchild", parent_task_id=child["id"])
    unrelated = _create(client, "task", title="Unrelated")

    _delete(client, "task", parent["id"])
    assert _ids(client.get("/get-tasks")) == {unrelated["id"]}
    trash = {i["id"]: i for i in _trash(client)}
    assert set(trash) == {parent["id"], child["id"], grandchild["id"]}
    assert trash[child["id"]]["parent_task_id"] == parent["id"]

    client.patch(f"/restore-task/{parent['id']}")
    assert _ids(client.get("/get-tasks")) == {parent["id"], child["id"], grandchild["id"], unrelated["id"]}
    assert _trash(client) == []


def test_restoring_a_task_keeps_subtasks_deleted_on_their_own(client):
    parent = _create(client, "task", title="Parent")
    deleted_first = _create(client, "task", title="Dropped step", parent_task_id=parent["id"])
    sibling = _create(client, "task", title="Kept step", parent_task_id=parent["id"])

    _delete(client, "task", deleted_first["id"])
    _delete(client, "task", parent["id"])
    client.patch(f"/restore-task/{parent['id']}")

    assert _ids(client.get("/get-tasks")) == {parent["id"], sibling["id"]}
    assert [i["id"] for i in _trash(client)] == [deleted_first["id"]]


def test_restoring_a_subtask_restores_its_deleted_ancestors_only(client):
    root = _create(client, "task", title="Root")
    parent = _create(client, "task", title="Parent", parent_task_id=root["id"])
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])
    sibling = _create(client, "task", title="Sibling", parent_task_id=parent["id"])

    _delete(client, "task", root["id"])
    assert client.patch(f"/restore-task/{child['id']}").status_code == 200

    assert _ids(client.get("/get-tasks")) == {root["id"], parent["id"], child["id"]}
    assert [i["id"] for i in _trash(client)] == [sibling["id"]]


def test_restoring_a_subtask_whose_parent_was_linked_meanwhile_keeps_it_under_that_parent(client):
    parent = _create(client, "task", title="Parent")
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])
    other = _create(client, "task", title="Other")

    _delete(client, "task", child["id"])
    resp = client.put(f"/update-linked-tasks/{other['id']}", json={"children": [{"id": parent["id"]}]})
    assert resp.status_code == 200, resp.text
    restored = client.patch(f"/restore-task/{child['id']}").json()

    assert (restored["parent_task_id"], restored["position"]) == (parent["id"], child["position"])
    tasks = {t["id"]: t for t in client.get("/get-tasks").json()}
    assert tasks[parent["id"]]["parent_task_id"] == other["id"]


def test_restoring_a_subtask_restores_a_parent_deleted_after_relinking(client):
    parent = _create(client, "task", title="Parent")
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])
    other = _create(client, "task", title="Other")

    _delete(client, "task", child["id"])
    client.put(f"/update-task/{parent['id']}", json={"title": "Parent", "parent_task_id": other["id"]})
    _delete(client, "task", parent["id"])
    restored = client.patch(f"/restore-task/{child['id']}").json()

    assert restored["parent_task_id"] == parent["id"]
    tasks = {t["id"]: t for t in client.get("/get-tasks").json()}
    assert set(tasks) == {child["id"], parent["id"], other["id"]}
    assert tasks[parent["id"]]["parent_task_id"] == other["id"]
    assert _trash(client) == []


def test_deleting_a_middle_task_takes_only_the_tree_below_it(client):
    root = _create(client, "task", title="Root")
    middle = _create(client, "task", title="Middle", parent_task_id=root["id"])
    leaf = _create(client, "task", title="Leaf", parent_task_id=middle["id"])
    below_leaf = _create(client, "task", title="Below leaf", parent_task_id=leaf["id"])
    sibling = _create(client, "task", title="Sibling", parent_task_id=root["id"])

    _delete(client, "task", middle["id"])

    assert _ids(client.get("/get-tasks")) == {root["id"], sibling["id"]}
    assert {i["id"] for i in _trash(client)} == {middle["id"], leaf["id"], below_leaf["id"]}

    # Restoring the deepest task brings back the chain above it, not its relatives.
    restored = client.patch(f"/restore-task/{below_leaf['id']}").json()
    assert restored["parent_task_id"] == leaf["id"]
    assert _ids(client.get("/get-tasks")) == {root["id"], sibling["id"], middle["id"], leaf["id"], below_leaf["id"]}
    assert _trash(client) == []


def test_restoring_a_middle_task_brings_back_its_tree_and_its_deleted_ancestors(client):
    root = _create(client, "task", title="Root")
    middle = _create(client, "task", title="Middle", parent_task_id=root["id"])
    leaf = _create(client, "task", title="Leaf", parent_task_id=middle["id"])
    below_leaf = _create(client, "task", title="Below leaf", parent_task_id=leaf["id"])
    dropped = _create(client, "task", title="Dropped", parent_task_id=leaf["id"])

    _delete(client, "task", dropped["id"])
    _delete(client, "task", root["id"])
    client.patch(f"/restore-task/{middle['id']}")

    assert _ids(client.get("/get-tasks")) == {root["id"], middle["id"], leaf["id"], below_leaf["id"]}
    assert [i["id"] for i in _trash(client)] == [dropped["id"]]


def test_purging_a_task_takes_its_whole_tree_and_detaches_the_rest(client, db):
    root = _create(client, "task", title="Root")
    middle = _create(client, "task", title="Middle", parent_task_id=root["id"])
    leaf = _create(client, "task", title="Leaf", parent_task_id=middle["id"])
    below_leaf = _create(client, "task", title="Below leaf", parent_task_id=leaf["id"])
    dropped = _create(client, "task", title="Dropped", parent_task_id=leaf["id"])

    _delete(client, "task", dropped["id"])
    _delete(client, "task", root["id"])
    assert client.delete(f"/purge-task/{root['id']}").status_code == 204

    db.expire_all()
    for gone in (root, middle, leaf, below_leaf):
        assert db.get(Task, gone["id"]) is None
    [left] = _trash(client)
    assert (left["id"], left["parent_task_id"]) == (dropped["id"], None)
    assert client.patch(f"/restore-task/{dropped['id']}").json()["parent_task_id"] is None


def test_purging_a_task_takes_the_subtasks_deleted_with_it(client, db):
    parent = _create(client, "task", title="Parent")
    deleted_first = _create(client, "task", title="Dropped step", parent_task_id=parent["id"])
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])

    _delete(client, "task", deleted_first["id"])
    _delete(client, "task", parent["id"])
    assert client.delete(f"/purge-task/{parent['id']}").status_code == 204

    db.expire_all()
    assert db.get(Task, parent["id"]) is None
    assert db.get(Task, child["id"]) is None
    [left] = _trash(client)
    assert left["id"] == deleted_first["id"]
    assert left["parent_task_id"] is None


def test_expired_parent_purge_detaches_remaining_subtasks(client, db):
    parent = _row(db, "task", deleted_ago=TRASH_RETENTION + timedelta(days=1))
    later = _row(db, "task", parent_task_id=parent.id, deleted_ago=timedelta(days=1))
    parent_id = parent.id

    [item] = _trash(client)
    assert item["id"] == later.id
    db.expire_all()
    assert db.get(Task, parent_id) is None
    assert client.patch(f"/restore-task/{later.id}").json()["parent_task_id"] is None


def test_expired_tree_purge_removes_every_level(client, db):
    expired = TRASH_RETENTION + timedelta(days=1)
    root = _row(db, "task", deleted_ago=expired)
    middle = _row(db, "task", parent_task_id=root.id, deleted_ago=expired)
    leaf = _row(db, "task", parent_task_id=middle.id, deleted_ago=expired)
    later = _row(db, "task", parent_task_id=leaf.id, deleted_ago=timedelta(days=1))
    ids = [root.id, middle.id, leaf.id]

    [item] = _trash(client)
    assert (item["id"], item["parent_task_id"]) == (later.id, None)
    db.expire_all()
    assert all(db.get(Task, i) is None for i in ids)


def test_deleted_task_cannot_be_a_parent(client):
    parent = _create(client, "task", title="Parent")
    _delete(client, "task", parent["id"])
    resp = client.post("/create-task", json={"title": "Child", "parent_task_id": parent["id"]})
    assert resp.status_code == 400


def test_deleted_task_does_not_hold_its_priority(client):
    deleted = _create(client, "task", title="Old", priority=1)
    _delete(client, "task", deleted["id"])

    resp = client.post("/create-task", json={"title": "New", "priority": 1})
    assert resp.status_code == 200, resp.text

    restored = client.patch(f"/restore-task/{deleted['id']}").json()
    assert restored["deleted_at"] is None
    assert restored["priority"] is None


# ── Stats, debrief and account cleanup ───────────────────────────────────────


def test_debrief_leaves_out_deleted_tasks_notes_and_habits(client, db):
    today = date.today()
    due_today = datetime.combine(today, datetime.min.time())
    now = datetime.now(UTC).replace(tzinfo=None)
    _row(db, "task", title="Due today", due_date=due_today)
    _row(db, "task", title="Deleted due today", due_date=due_today, estimated_time=3, deleted_ago=timedelta(0))
    _row(db, "task", title="Deleted done", completed=True, completed_date=now, deleted_ago=timedelta(0))
    _row(db, "habit", title="Live habit")
    _row(db, "habit", title="Deleted habit", deleted_ago=timedelta(0))
    deleted_note = _row(db, "note", title="Deleted note", deleted_ago=timedelta(0))
    db.add(NoteSession(note_id=deleted_note.id, user_id=USER_ID, started_at=now - timedelta(minutes=30), ended_at=now))
    db.commit()

    report = client.get("/daily-debrief", params={"local_date": today.isoformat()}).json()
    assert [t["title"] for t in report["due_today_tasks"]] == ["Due today"]
    assert report["completed_today_tasks"] == []
    assert report["workload"]["committed_minutes"] == 0
    assert [h["title"] for h in report["habit_status"]] == ["Live habit"]
    assert report["notes_worked_today"] == []


def test_verify_streaks_skips_deleted_habits(client, db):
    deleted = _row(db, "habit", current_streak=5, max_streak=5, deleted_ago=timedelta(0))
    assert client.post("/verify-streaks").json() == {"reset_count": 0}
    db.refresh(deleted)
    assert deleted.current_streak == 5


def test_account_deletion_and_demo_seed_handle_deleted_rows(client, db):
    for kind in KINDS:
        _row(db, kind, deleted_ago=timedelta(days=1))

    seed_demo_data(db, USER_ID, local_date=date.today().isoformat())
    assert _trash(client) == []
    assert client.get("/get-tasks").json()

    for kind in KINDS:
        _row(db, kind, deleted_ago=timedelta(days=1))
    delete_user_rows(db, USER_ID)
    db.commit()
    for model in (Task, Note, Tag, Habit):
        assert db.query(model).filter(model.user_id == USER_ID).count() == 0
