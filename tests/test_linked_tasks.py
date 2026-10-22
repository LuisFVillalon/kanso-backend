"""Linked tasks: saving a parent's ordered links in one go, nesting to any depth, and the loop and archive guards."""

import pytest

from app.models.task_model import Task
from app.schemas.limits import LINKED_TASKS_MAX
from tests.conftest import OTHER_USER_ID

SCHOOL = {"name": "School", "color": "#8b5cf6"}


def _create(client, **fields):
    resp = client.post("/create-task", json={"title": "Task", **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_linked(client, parent_id, children):
    return client.put(f"/update-linked-tasks/{parent_id}", json={"children": children})


def _linked(client, parent_id, children):
    resp = _set_linked(client, parent_id, children)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tasks(client, archived=False):
    resp = client.get("/get-tasks", params={"archived": archived})
    assert resp.status_code == 200, resp.text
    return {t["id"]: t for t in resp.json()}


def _chain(client, n):
    """n tasks, each linked under the one before it."""
    tasks = [_create(client, title="Level 0")]
    for level in range(1, n):
        tasks.append(_create(client, title=f"Level {level}", parent_task_id=tasks[-1]["id"]))
    return tasks


# ── Saving links ─────────────────────────────────────────────────────────────


def test_links_existing_and_new_tasks_in_order(client):
    parent = _create(client, title="Prepare for phone screen", tags=[SCHOOL], due_date="2026-10-30")
    existing = _create(client, title="Update résumé", priority=2)

    body = _linked(client, parent["id"], [{"title": "  Ask for a reference  "}, {"id": existing["id"]}])

    assert body["parent"]["id"] == parent["id"]
    new, moved = body["children"]
    assert (new["title"], new["position"], new["parent_task_id"]) == ("Ask for a reference", 0, parent["id"])
    assert (moved["id"], moved["position"], moved["parent_task_id"]) == (existing["id"], 1, parent["id"])
    # New tasks copy the parent's tags only; existing ones keep their own fields.
    assert [t["name"] for t in new["tags"]] == ["School"]
    assert new["due_date"] is None and new["priority"] is None and new["completed"] is False
    assert moved["priority"] == 2

    tasks = _tasks(client)
    assert tasks[new["id"]]["parent_task_id"] == parent["id"]
    assert tasks[parent["id"]]["position"] is None


def test_saving_again_reorders(client):
    parent = _create(client)
    a, b, c = (_create(client, title=t) for t in "abc")
    _linked(client, parent["id"], [{"id": a["id"]}, {"id": b["id"]}, {"id": c["id"]}])

    body = _linked(client, parent["id"], [{"id": c["id"]}, {"id": a["id"]}, {"id": b["id"]}])

    assert [(t["id"], t["position"]) for t in body["children"]] == [(c["id"], 0), (a["id"], 1), (b["id"], 2)]
    assert {i: t["position"] for i, t in _tasks(client).items() if i != parent["id"]} == {
        c["id"]: 0, a["id"]: 1, b["id"]: 2
    }


def test_linking_a_task_moves_it_from_its_old_parent(client):
    old_parent = _create(client, title="Old parent")
    new_parent = _create(client, title="New parent")
    child = _create(client, title="Mock interview", parent_task_id=old_parent["id"])

    _linked(client, new_parent["id"], [{"id": child["id"]}])

    assert _tasks(client)[child["id"]]["parent_task_id"] == new_parent["id"]
    assert _linked(client, old_parent["id"], [])["children"] == []


def test_tasks_left_out_are_unlinked_not_deleted(client):
    parent = _create(client)
    kept = _create(client, title="Kept", parent_task_id=parent["id"])
    dropped = _create(client, title="Dropped", parent_task_id=parent["id"])

    body = _linked(client, parent["id"], [{"id": kept["id"]}])

    assert [t["id"] for t in body["children"]] == [kept["id"]]
    left = _tasks(client)[dropped["id"]]
    assert (left["parent_task_id"], left["position"], left["deleted_at"]) == (None, None, None)

    assert _linked(client, parent["id"], [])["children"] == []
    assert _tasks(client)[kept["id"]]["parent_task_id"] is None


def test_archived_linked_tasks_stay_linked(client):
    # The client never sees archived tasks, so leaving them out can't mean "unlink".
    parent = _create(client)
    archived = _create(client, title="Archived step", parent_task_id=parent["id"])
    client.patch(f"/archive-task/{archived['id']}")

    _linked(client, parent["id"], [{"title": "New step"}])

    assert _tasks(client, archived=True)[archived["id"]]["parent_task_id"] == parent["id"]


def test_new_tasks_skip_the_parents_deleted_tags(client):
    tag = client.post("/create-tags", json={"name": "Old"}).json()
    parent = _create(client, tags=[SCHOOL, {"name": "Old"}])
    client.delete(f"/del-tag/{tag['id']}")

    [new] = _linked(client, parent["id"], [{"title": "Step"}])["children"]

    assert [t["name"] for t in new["tags"]] == ["School"]


def test_a_rejected_save_changes_nothing(client, db):
    parent = _create(client)
    first = _create(client, title="First")
    foreign = Task(title="Theirs", user_id=OTHER_USER_ID)
    db.add(foreign)
    db.commit()
    before = set(_tasks(client))

    resp = _set_linked(client, parent["id"], [{"id": first["id"]}, {"title": "New"}, {"id": foreign.id}])
    assert resp.status_code == 400
    db.rollback()  # what closing the request's session does

    tasks = _tasks(client)
    assert set(tasks) == before
    assert tasks[first["id"]]["parent_task_id"] is None


# ── Guards ───────────────────────────────────────────────────────────────────


def test_missing_or_foreign_parent_is_404(client, db):
    foreign = Task(title="Theirs", user_id=OTHER_USER_ID)
    db.add(foreign)
    db.commit()
    assert _set_linked(client, foreign.id, []).status_code == 404
    assert _set_linked(client, 999999, []).status_code == 404


def test_missing_foreign_or_deleted_children_are_rejected(client, db):
    parent = _create(client)
    foreign = Task(title="Theirs", user_id=OTHER_USER_ID)
    db.add(foreign)
    db.commit()
    deleted = _create(client, title="Deleted")
    client.delete(f"/del-task/{deleted['id']}")

    for child_id in (foreign.id, deleted["id"], 999999):
        assert _set_linked(client, parent["id"], [{"id": child_id}]).status_code == 400
    db.rollback()
    db.refresh(foreign)
    assert foreign.parent_task_id is None


def test_a_task_cannot_be_listed_twice_or_linked_to_itself(client):
    parent = _create(client)
    child = _create(client)
    assert _set_linked(client, parent["id"], [{"id": child["id"]}, {"id": child["id"]}]).status_code == 400
    assert _set_linked(client, parent["id"], [{"id": parent["id"]}]).status_code == 400


@pytest.mark.parametrize(
    "children",
    [
        [{}],
        [{"title": "   "}],
        [{"id": 1, "title": "Both"}],
        [{"title": "x" * 501}],
        [{"title": "Step"}] * (LINKED_TASKS_MAX + 1),
    ],
)
def test_invalid_bodies_are_422(client, children):
    parent = _create(client)
    assert _set_linked(client, parent["id"], children).status_code == 422


def test_loops_are_rejected(client):
    parent = _create(client, title="Parent")
    child = _create(client, title="Child", parent_task_id=parent["id"])

    resp = _set_linked(client, child["id"], [{"id": parent["id"]}])
    assert resp.status_code == 400
    assert "loop" in resp.json()["detail"]
    resp = client.put(f"/update-task/{parent['id']}", json={"title": "Parent", "parent_task_id": child["id"]})
    assert resp.status_code == 400
    assert "loop" in resp.json()["detail"]
    resp = client.put(f"/update-task/{parent['id']}", json={"title": "Parent", "parent_task_id": parent["id"]})
    assert resp.status_code == 400


def test_loops_through_deeper_trees_are_rejected(client):
    root, middle, leaf = _chain(client, 3)

    for task, under in ((root, leaf), (root, middle), (middle, leaf)):
        resp = client.put(f"/update-task/{task['id']}", json={"title": task["title"], "parent_task_id": under["id"]})
        assert resp.status_code == 400
        assert "loop" in resp.json()["detail"]
        resp = _set_linked(client, under["id"], [{"id": task["id"]}])
        assert resp.status_code == 400
        assert "loop" in resp.json()["detail"]

    tasks = _tasks(client)
    assert [tasks[t["id"]]["parent_task_id"] for t in (root, middle, leaf)] == [None, root["id"], middle["id"]]


def test_archived_parents_cannot_take_linked_tasks(client):
    parent = _create(client, title="Parent")
    client.patch(f"/archive-task/{parent['id']}")
    other = _create(client, title="Other")

    assert _set_linked(client, parent["id"], [{"id": other["id"]}]).status_code == 400
    assert _set_linked(client, parent["id"], [{"title": "New"}]).status_code == 400
    assert client.post("/create-task", json={"title": "New", "parent_task_id": parent["id"]}).status_code == 400
    resp = client.put(f"/update-task/{other['id']}", json={"title": "Other", "parent_task_id": parent["id"]})
    assert resp.status_code == 400


def test_archived_tasks_cannot_be_linked(client):
    parent = _create(client)
    archived = _create(client, title="Archived")
    client.patch(f"/archive-task/{archived['id']}")

    assert _set_linked(client, parent["id"], [{"id": archived["id"]}]).status_code == 400


# ── Position through create and update ───────────────────────────────────────


def test_create_and_update_append_to_the_end(client):
    parent = _create(client, title="Parent")
    first = _create(client, title="First", parent_task_id=parent["id"])
    second = _create(client, title="Second", parent_task_id=parent["id"])
    loose = _create(client, title="Loose")
    assert (first["position"], second["position"], loose["position"]) == (0, 1, None)

    moved = client.put(f"/update-task/{loose['id']}", json={"title": "Loose", "parent_task_id": parent["id"]})
    assert moved.json()["position"] == 2

    unlinked = client.put(f"/update-task/{first['id']}", json={"title": "First"})
    assert (unlinked.json()["parent_task_id"], unlinked.json()["position"]) == (None, None)


def test_update_keeps_the_position_when_the_parent_is_unchanged(client):
    parent = _create(client)
    a, b = _create(client, title="a"), _create(client, title="b")
    _linked(client, parent["id"], [{"id": b["id"]}, {"id": a["id"]}])

    resp = client.put(f"/update-task/{a['id']}", json={"title": "a, renamed", "parent_task_id": parent["id"]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["position"] == 1


def test_siblings_are_ordered_at_every_depth(client):
    _, middle = _chain(client, 2)
    first = _create(client, title="First", parent_task_id=middle["id"])
    second = _create(client, title="Second", parent_task_id=middle["id"])
    assert (first["position"], second["position"]) == (0, 1)

    body = _linked(client, middle["id"], [{"id": second["id"]}, {"title": "Third"}, {"id": first["id"]}])

    assert [(c["title"], c["position"]) for c in body["children"]] == [("Second", 0), ("Third", 1), ("First", 2)]


# ── Nesting ──────────────────────────────────────────────────────────────────


def test_linked_tasks_nest_to_any_depth(client):
    root, child, grandchild = _chain(client, 3)
    [great] = _linked(client, grandchild["id"], [{"title": "Great-grandchild"}])["children"]
    deeper = _create(client, title="Deeper", parent_task_id=great["id"])

    tasks = _tasks(client)
    assert [tasks[t["id"]]["parent_task_id"] for t in (root, child, grandchild, great, deeper)] == [
        None, root["id"], child["id"], grandchild["id"], great["id"]
    ]
    assert (tasks[great["id"]]["position"], tasks[deeper["id"]]["position"]) == (0, 0)


def test_a_task_with_linked_tasks_moves_with_them(client):
    root, middle, leaf = _chain(client, 3)
    other_root, other_child = _chain(client, 2)

    resp = client.put(f"/update-task/{middle['id']}", json={"title": "Level 1", "parent_task_id": other_child["id"]})
    assert resp.status_code == 200, resp.text
    tasks = _tasks(client)
    assert tasks[middle["id"]]["parent_task_id"] == other_child["id"]
    assert tasks[leaf["id"]]["parent_task_id"] == middle["id"]

    # Moving it back the other way, through the linked-tasks save.
    body = _linked(client, root["id"], [{"id": middle["id"]}])
    assert body["children"][0]["parent_task_id"] == root["id"]
    tasks = _tasks(client)
    assert tasks[leaf["id"]]["parent_task_id"] == middle["id"]
    assert other_root["id"] in tasks


def test_a_nested_task_stores_every_field_a_top_level_task_does(client):
    _, child = _chain(client, 2)
    fields = {
        "title": "Draft outline",
        "description": "Two pages, double spaced",
        "category": "Essay",
        "priority": 7,
        "due_date": "2026-11-03",
        "due_time": "17:30:00",
        "estimated_time": 1.5,
        "reminder_minutes": 45,
        "canvas_assignment_id": 424242,
        "canvas_html_url": "https://canvas.example.edu/courses/1/assignments/424242",
        "canvas_due_at": "2026-11-04T06:59:00Z",
        "tags": [SCHOOL],
    }

    created = _create(client, parent_task_id=child["id"], **fields)
    stored = _tasks(client)[created["id"]]
    for body in (created, stored):
        assert body["parent_task_id"] == child["id"]
        for name, value in fields.items():
            if name == "tags":
                assert [t["name"] for t in body["tags"]] == ["School"]
            elif name != "canvas_due_at":
                assert body[name] == value, name

    updated = {**fields, "title": "Final outline", "priority": 8, "due_date": "2026-11-05"}
    resp = client.put(f"/update-task/{created['id']}", json={**updated, "parent_task_id": child["id"]})
    assert resp.status_code == 200, resp.text
    stored = _tasks(client)[created["id"]]
    assert (stored["title"], stored["priority"], stored["due_date"]) == ("Final outline", 8, "2026-11-05")
    assert stored["canvas_assignment_id"] == 424242


def test_tasks_name_their_parent(client):
    root, child, grandchild = _chain(client, 3)
    client.put(f"/update-task/{root['id']}", json={"title": "Renamed root"})
    client.patch(f"/archive-task/{grandchild['id']}")

    tasks = _tasks(client)
    assert tasks[root["id"]]["parent_task"] is None
    assert tasks[child["id"]]["parent_task"] == {"id": root["id"], "title": "Renamed root"}
    # An archived task names its parent even though the parent is in the other list.
    archived = _tasks(client, archived=True)
    assert archived[grandchild["id"]]["parent_task"] == {"id": child["id"], "title": "Level 1"}

    [new] = _linked(client, child["id"], [{"title": "New"}])["children"]
    assert new["parent_task"] == {"id": child["id"], "title": "Level 1"}
    created = _create(client, title="Created", parent_task_id=new["id"])
    assert created["parent_task"] == {"id": new["id"], "title": "New"}
