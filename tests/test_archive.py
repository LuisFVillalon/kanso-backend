"""Archiving and restoring tasks, notes and tags."""

import pytest

from app.models.note_model import Note
from app.models.tag_model import Tag
from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID

# kind -> (create path, create payload, list path, delete path, model)
KINDS = {
    "task": ("/create-task", {"title": "Write report"}, "/get-tasks", "/del-task", Task),
    "note": ("/create-note", {"title": "Lecture notes"}, "/get-notes", "/del-note", Note),
    "tag": ("/create-tags", {"name": "School"}, "/get-tags", "/del-tag", Tag),
}


def _create(client, kind, **overrides):
    path, payload, *_ = KINDS[kind]
    resp = client.post(path, json={**payload, **overrides})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


@pytest.mark.parametrize("kind", KINDS)
def test_new_rows_are_active(client, kind):
    assert _create(client, kind)["archived_at"] is None


@pytest.mark.parametrize("kind", KINDS)
def test_list_returns_active_by_default_and_archived_on_request(client, kind):
    list_path = KINDS[kind][2]
    active = _create(client, kind)
    archived = _create(client, kind, **({"name": "Old"} if kind == "tag" else {}))
    client.patch(f"/archive-{kind}/{archived['id']}")

    assert _ids(client.get(list_path)) == {active["id"]}
    assert _ids(client.get(list_path, params={"archived": "false"})) == {active["id"]}
    assert _ids(client.get(list_path, params={"archived": "true"})) == {archived["id"]}


@pytest.mark.parametrize("kind", KINDS)
def test_archive_and_unarchive_set_and_clear_archived_at(client, kind):
    row = _create(client, kind)

    resp = client.patch(f"/archive-{kind}/{row['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == row["id"]
    assert resp.json()["archived_at"] is not None

    resp = client.patch(f"/unarchive-{kind}/{row['id']}")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None


@pytest.mark.parametrize("kind", KINDS)
def test_archive_and_unarchive_are_idempotent(client, kind):
    row = _create(client, kind)
    first = client.patch(f"/archive-{kind}/{row['id']}").json()["archived_at"]
    second = client.patch(f"/archive-{kind}/{row['id']}")
    assert second.status_code == 200
    assert second.json()["archived_at"] == first

    client.patch(f"/unarchive-{kind}/{row['id']}")
    again = client.patch(f"/unarchive-{kind}/{row['id']}")
    assert again.status_code == 200
    assert again.json()["archived_at"] is None


@pytest.mark.parametrize("action", ["archive", "unarchive"])
@pytest.mark.parametrize("kind", KINDS)
def test_archive_endpoints_404_for_missing_or_foreign_rows(client, db, kind, action):
    model = KINDS[kind][4]
    fields = {"name": "Theirs"} if kind == "tag" else {"title": "Theirs"}
    foreign = model(user_id=OTHER_USER_ID, **fields)
    db.add(foreign)
    db.commit()

    assert client.patch(f"/{action}-{kind}/{foreign.id}").status_code == 404
    assert client.patch(f"/{action}-{kind}/999999").status_code == 404
    db.refresh(foreign)
    assert foreign.archived_at is None


@pytest.mark.parametrize("kind", KINDS)
def test_delete_works_on_archived_rows(client, kind):
    _, _, list_path, delete_path, _ = KINDS[kind]
    row = _create(client, kind)
    client.patch(f"/archive-{kind}/{row['id']}")

    assert client.delete(f"{delete_path}/{row['id']}").status_code == 200
    assert _ids(client.get(list_path, params={"archived": "true"})) == set()


def test_archiving_a_task_cascades_to_its_subtasks(client):
    parent = _create(client, "task", title="Parent")
    child = _create(client, "task", title="Child", parent_task_id=parent["id"])
    grandchild = _create(client, "task", title="Grandchild", parent_task_id=child["id"])
    unrelated = _create(client, "task", title="Unrelated")

    client.patch(f"/archive-task/{parent['id']}")
    assert _ids(client.get("/get-tasks")) == {unrelated["id"]}
    assert _ids(client.get("/get-tasks", params={"archived": "true"})) == {parent["id"], child["id"], grandchild["id"]}

    client.patch(f"/unarchive-task/{parent['id']}")
    assert _ids(client.get("/get-tasks")) == {parent["id"], child["id"], grandchild["id"], unrelated["id"]}
    assert _ids(client.get("/get-tasks", params={"archived": "true"})) == set()


def test_restoring_a_task_keeps_subtasks_archived_on_their_own(client):
    parent = _create(client, "task", title="Parent")
    archived_first = _create(client, "task", title="Dropped step", parent_task_id=parent["id"])
    sibling = _create(client, "task", title="Kept step", parent_task_id=parent["id"])

    client.patch(f"/archive-task/{archived_first['id']}")
    client.patch(f"/archive-task/{parent['id']}")
    client.patch(f"/unarchive-task/{parent['id']}")

    assert _ids(client.get("/get-tasks")) == {parent["id"], sibling["id"]}
    assert _ids(client.get("/get-tasks", params={"archived": "true"})) == {archived_first["id"]}


def test_archived_tag_stays_attached_to_tasks_and_notes(client):
    task = _create(client, "task", tags=[{"name": "School", "color": "#8b5cf6"}])
    note = _create(client, "note", tags=[{"name": "School", "color": "#8b5cf6"}])
    tag_id = task["tags"][0]["id"]

    archived = client.patch(f"/archive-tag/{tag_id}").json()
    assert _ids(client.get("/get-tags")) == set()

    for list_path, row in (("/get-tasks", task), ("/get-notes", note)):
        listed = {r["id"]: r for r in client.get(list_path).json()}
        assert row["id"] in listed, "archiving a tag must not hide the items carrying it"
        tags = listed[row["id"]]["tags"]
        assert [t["id"] for t in tags] == [tag_id]
        assert tags[0]["archived_at"] == archived["archived_at"]


def test_tag_name_conflict_with_archived_tag_mentions_the_archive(client):
    tag = _create(client, "tag", name="School")
    client.patch(f"/archive-tag/{tag['id']}")

    resp = client.post("/create-tags", json={"name": "School"})
    assert resp.status_code == 409
    assert "archive" in resp.json()["detail"]


def test_archived_task_does_not_hold_its_priority(client):
    archived = _create(client, "task", title="Old", priority=1)
    client.patch(f"/archive-task/{archived['id']}")

    resp = client.post("/create-task", json={"title": "New", "priority": 1})
    assert resp.status_code == 200, resp.text

    # Restoring would duplicate priority 1, so the restored task gives it up.
    restored = client.patch(f"/unarchive-task/{archived['id']}").json()
    assert restored["archived_at"] is None
    assert restored["priority"] is None


def test_restored_task_keeps_a_free_priority(client):
    task = _create(client, "task", priority=2)
    client.patch(f"/archive-task/{task['id']}")
    assert client.patch(f"/unarchive-task/{task['id']}").json()["priority"] == 2
