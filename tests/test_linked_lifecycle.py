"""Deleting, archiving and completing a task that has linked tasks: ?linked=keep|cascade and ?with_linked=true."""

import pytest

from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID


def _create(client, **fields):
    resp = client.post("/create-task", json={"title": "Task", **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _family(client, n=2):
    """A parent task with n linked tasks."""
    parent = _create(client, title="Parent")
    children = [_create(client, title=f"Child {i}", parent_task_id=parent["id"]) for i in range(n)]
    return parent, children


def _tree(client):
    """Root -> middle -> (leaf -> below leaf), the shape every nested test starts from."""
    root = _create(client, title="Root")
    middle = _create(client, title="Middle", parent_task_id=root["id"])
    leaf = _create(client, title="Leaf", parent_task_id=middle["id"])
    below_leaf = _create(client, title="Below leaf", parent_task_id=leaf["id"])
    return root, middle, leaf, below_leaf


def _tasks(client, archived=False):
    resp = client.get("/get-tasks", params={"archived": archived})
    assert resp.status_code == 200, resp.text
    return {t["id"]: t for t in resp.json()}


def _trash_ids(client):
    resp = client.get("/get-trash")
    assert resp.status_code == 200, resp.text
    return {i["id"] for i in resp.json() if i["kind"] == "task"}


def _delete(client, task_id, **params):
    return client.delete(f"/del-task/{task_id}", params=params)


def _archive(client, task_id, **params):
    return client.patch(f"/archive-task/{task_id}", params=params)


def _complete(client, task_id, **params):
    return client.patch(f"/update-task-status/{task_id}", params=params)


def _foreign_child(db, parent_id):
    """Another user's task pointing at one of ours, which no endpoint should touch."""
    foreign = Task(title="Theirs", user_id=OTHER_USER_ID, parent_task_id=parent_id)
    db.add(foreign)
    db.commit()
    return foreign


# ── Delete ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("params", [{}, {"linked": "cascade"}])
def test_delete_cascades_by_default_and_with_cascade(client, params):
    parent, children = _family(client)

    assert _delete(client, parent["id"], **params).status_code == 200

    assert {parent["id"], *(c["id"] for c in children)} <= _trash_ids(client)
    assert not set(_tasks(client)) & {c["id"] for c in children}


def test_delete_keep_unlinks_linked_tasks_and_trashes_only_the_parent(client):
    parent, children = _family(client)
    archived_child = _create(client, title="Archived child", parent_task_id=parent["id"])
    _archive(client, archived_child["id"])

    resp = _delete(client, parent["id"], linked="keep")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_at"] is not None

    trash = _trash_ids(client)
    assert parent["id"] in trash
    tasks = _tasks(client)
    for c in children:
        assert c["id"] not in trash
        assert (tasks[c["id"]]["parent_task_id"], tasks[c["id"]]["position"]) == (None, None)
    archived = _tasks(client, archived=True)[archived_child["id"]]
    assert archived["parent_task_id"] is None


def test_restoring_a_parent_deleted_with_keep_leaves_its_old_linked_tasks_standalone(client):
    parent, children = _family(client)
    _delete(client, parent["id"], linked="keep")

    assert client.patch(f"/restore-task/{parent['id']}").status_code == 200

    tasks = _tasks(client)
    assert parent["id"] in tasks
    assert all(tasks[c["id"]]["parent_task_id"] is None for c in children)


def test_delete_keep_on_a_middle_task_unlinks_its_children_with_their_own_subtasks(client):
    root, middle, leaf, below_leaf = _tree(client)

    assert _delete(client, middle["id"], linked="keep").status_code == 200

    assert _trash_ids(client) == {middle["id"]}
    tasks = _tasks(client)
    assert (tasks[leaf["id"]]["parent_task_id"], tasks[leaf["id"]]["parent_task"]) == (None, None)
    assert tasks[below_leaf["id"]]["parent_task_id"] == leaf["id"]
    assert root["id"] in tasks


def test_trashed_tasks_name_their_parent_whether_live_or_trashed(client):
    root, middle, leaf, below_leaf = _tree(client)
    note = client.post("/create-note", json={"title": "Lecture notes"}).json()

    assert _delete(client, leaf["id"]).status_code == 200
    assert _delete(client, root["id"], linked="keep").status_code == 200
    assert client.delete(f"/del-note/{note['id']}").status_code == 200

    resp = client.get("/get-trash")
    assert resp.status_code == 200, resp.text
    parents = {(i["kind"], i["id"]): i["parent_task"] for i in resp.json()}
    assert parents == {
        ("task", leaf["id"]): {"id": middle["id"], "title": "Middle"},
        ("task", below_leaf["id"]): {"id": leaf["id"], "title": "Leaf"},
        ("task", root["id"]): None,
        ("note", note["id"]): None,
    }


# ── Archive ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("params", [{}, {"linked": "cascade"}])
def test_archive_cascades_by_default_and_with_cascade(client, params):
    parent, children = _family(client)

    assert _archive(client, parent["id"], **params).status_code == 200

    archived = _tasks(client, archived=True)
    assert {parent["id"], *(c["id"] for c in children)} <= set(archived)
    assert all(archived[c["id"]]["parent_task_id"] == parent["id"] for c in children)


def test_archive_keep_unlinks_active_linked_tasks_and_archives_only_the_parent(client):
    parent, children = _family(client)
    archived_child = _create(client, title="Archived on its own", parent_task_id=parent["id"])
    _archive(client, archived_child["id"])

    resp = _archive(client, parent["id"], linked="keep")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_at"] is not None

    tasks = _tasks(client)
    for c in children:
        assert (tasks[c["id"]]["parent_task_id"], tasks[c["id"]]["position"]) == (None, None)
    archived = _tasks(client, archived=True)
    assert parent["id"] in archived
    # A linked task archived before the parent is hidden from the client, so it stays linked.
    assert archived[archived_child["id"]]["parent_task_id"] == parent["id"]


def test_archive_and_unarchive_reach_every_level(client):
    root, middle, leaf, below_leaf = _tree(client)
    on_its_own = _create(client, title="Archived on its own", parent_task_id=leaf["id"])
    _archive(client, on_its_own["id"])

    assert _archive(client, middle["id"]).status_code == 200
    assert set(_tasks(client)) == {root["id"]}
    assert {middle["id"], leaf["id"], below_leaf["id"]} <= set(_tasks(client, archived=True))

    # Unarchiving the deepest task restores the archived chain above it.
    assert client.patch(f"/unarchive-task/{below_leaf['id']}").status_code == 200
    assert set(_tasks(client)) == {root["id"], middle["id"], leaf["id"], below_leaf["id"]}

    _archive(client, middle["id"])
    assert client.patch(f"/unarchive-task/{middle['id']}").status_code == 200
    assert set(_tasks(client)) == {root["id"], middle["id"], leaf["id"], below_leaf["id"]}
    assert set(_tasks(client, archived=True)) == {on_its_own["id"]}


def test_archive_keep_on_a_middle_task_unlinks_its_children_with_their_own_subtasks(client):
    root, middle, leaf, below_leaf = _tree(client)

    assert _archive(client, middle["id"], linked="keep").status_code == 200

    tasks = _tasks(client)
    assert set(tasks) == {root["id"], leaf["id"], below_leaf["id"]}
    assert tasks[leaf["id"]]["parent_task_id"] is None
    assert tasks[below_leaf["id"]]["parent_task_id"] == leaf["id"]


@pytest.mark.parametrize("call", [_delete, _archive])
def test_an_unknown_linked_mode_is_rejected(client, call):
    parent, children = _family(client, n=1)

    assert call(client, parent["id"], linked="orphan").status_code == 422

    tasks = _tasks(client)
    assert tasks[children[0]["id"]]["parent_task_id"] == parent["id"]


# ── Complete ─────────────────────────────────────────────────────────────────


def test_with_linked_completes_the_parent_and_its_open_linked_tasks(client):
    parent, (open_child, done_child) = _family(client)
    _complete(client, done_child["id"])
    done_before = _tasks(client)[done_child["id"]]["completed_date"]
    archived_child = _create(client, title="Archived", parent_task_id=parent["id"])
    _archive(client, archived_child["id"])

    resp = _complete(client, parent["id"], with_linked="true")
    assert resp.status_code == 200, resp.text
    assert resp.json()["completed"] is True

    tasks = _tasks(client)
    assert tasks[open_child["id"]]["completed"] is True
    assert tasks[open_child["id"]]["completed_date"] == tasks[parent["id"]]["completed_date"]
    # Already-done linked tasks keep their completion time; archived ones are left alone.
    assert tasks[done_child["id"]]["completed_date"] == done_before
    assert _tasks(client, archived=True)[archived_child["id"]]["completed"] is False


def test_with_linked_completes_open_linked_tasks_at_every_depth(client):
    root, middle, leaf, below_leaf = _tree(client)
    archived = _create(client, title="Archived", parent_task_id=leaf["id"])
    _archive(client, archived["id"])

    assert _complete(client, middle["id"], with_linked="true").status_code == 200

    tasks = _tasks(client)
    assert [tasks[t["id"]]["completed"] for t in (root, middle, leaf, below_leaf)] == [False, True, True, True]
    assert _tasks(client, archived=True)[archived["id"]]["completed"] is False


def test_without_with_linked_only_the_parent_is_completed(client):
    parent, children = _family(client)

    assert _complete(client, parent["id"]).json()["completed"] is True

    tasks = _tasks(client)
    assert all(tasks[c["id"]]["completed"] is False for c in children)


def test_reopening_with_linked_leaves_linked_tasks_completed(client):
    parent, children = _family(client)
    _complete(client, parent["id"], with_linked="true")

    resp = _complete(client, parent["id"], with_linked="true")
    assert resp.json()["completed"] is False

    tasks = _tasks(client)
    assert all(tasks[c["id"]]["completed"] is True for c in children)


# ── Ownership ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "call, params",
    [
        (_delete, {"linked": "keep"}),
        (_delete, {"linked": "cascade"}),
        (_archive, {"linked": "keep"}),
        (_archive, {"linked": "cascade"}),
        (_complete, {"with_linked": "true"}),
    ],
)
def test_other_users_tasks_are_never_touched(client, db, call, params):
    parent, _ = _family(client, n=1)
    foreign = _foreign_child(db, parent["id"])
    theirs = Task(title="Their parent", user_id=OTHER_USER_ID)
    db.add(theirs)
    db.commit()

    assert call(client, theirs.id, **params).status_code == 404
    assert call(client, parent["id"], **params).status_code == 200

    db.refresh(foreign)
    db.refresh(theirs)
    assert foreign.parent_task_id == parent["id"]
    assert (foreign.deleted_at, foreign.archived_at, foreign.completed) == (None, None, False)
    assert (theirs.deleted_at, theirs.archived_at, theirs.completed) == (None, None, False)
