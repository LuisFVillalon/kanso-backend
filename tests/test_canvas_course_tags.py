"""Each Canvas class's default tags for the tasks added from it."""
# The Canvas fixtures are shared with test_canvas_read.py by importing them.
# ruff: noqa: F811

from datetime import UTC, datetime

import pytest

from app.models.tag_model import Tag
from tests.conftest import OTHER_USER_ID, USER_ID
from tests.test_canvas_read import (  # noqa: F401 (fixtures)
    BASE_URL,
    TOKEN,
    canvas,
    connected,
    demo_client,
    fresh_state,
    sleeps,
)

OTHER_ACCOUNT_TOKEN = "7~Oo0aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3dOthr"


def _tag(db, name, user_id=USER_ID, **fields) -> Tag:
    tag = Tag(name=name, color="#123456", user_id=user_id, **fields)
    db.add(tag)
    db.flush()
    return tag


def _put(client, course_id, tag_ids):
    return client.put(f"/canvas/course-tags/{course_id}", json={"tag_ids": tag_ids})


def _saved(client) -> dict[int, list[str]]:
    response = client.get("/canvas/course-tags")
    assert response.status_code == 200, response.text
    return {course["course_id"]: [tag["name"] for tag in course["tags"]] for course in response.json()["courses"]}


def test_a_class_keeps_its_tags_in_order_until_cleared(connected, db, canvas):
    school, exam = _tag(db, "School"), _tag(db, "Exam prep")
    response = _put(connected, 11, [exam.id, school.id, exam.id])
    assert response.status_code == 200, response.text
    assert response.json()["courses"] == [{"course_id": 11, "tags": [
        {"id": exam.id, "name": "Exam prep", "color": "#123456", "user_id": USER_ID,
         "archived_at": None, "deleted_at": None},
        {"id": school.id, "name": "School", "color": "#123456", "user_id": USER_ID,
         "archived_at": None, "deleted_at": None},
    ]}]
    _put(connected, 12, [school.id])
    assert _saved(connected) == {11: ["Exam prep", "School"], 12: ["School"]}

    cleared = _put(connected, 11, [])
    assert [course["course_id"] for course in cleared.json()["courses"]] == [12]
    assert _saved(connected) == {12: ["School"]}
    # Saving tags never calls Canvas.
    assert canvas.requests == []


@pytest.mark.parametrize("whose", ["unknown", "other user", "deleted"])
def test_only_the_users_own_tags_can_be_saved(connected, db, whose):
    school = _tag(db, "School")
    _put(connected, 11, [school.id])
    if whose == "unknown":
        bad = school.id + 1000
    elif whose == "other user":
        bad = _tag(db, "Theirs", user_id=OTHER_USER_ID).id
    else:
        bad = _tag(db, "Gone", deleted_at=datetime.now(UTC)).id
    response = _put(connected, 11, [school.id, bad])
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_tags_invalid")
    assert _saved(connected) == {11: ["School"]}


def test_a_deleted_tag_drops_out_of_a_classes_tags(connected, db):
    school, exam = _tag(db, "School"), _tag(db, "Exam prep")
    _put(connected, 11, [school.id, exam.id])
    _put(connected, 12, [exam.id])
    connected.delete(f"/del-tag/{exam.id}")
    assert _saved(connected) == {11: ["School"]}


@pytest.mark.parametrize("body", [{"tag_ids": [0]}, {"tag_ids": list(range(1, 52))}, {}])
def test_a_malformed_body_is_refused(connected, body):
    assert connected.put("/canvas/course-tags/11", json=body).status_code == 422


def test_without_a_connection_there_are_no_tags_to_keep(client, db, canvas):
    school = _tag(db, "School")
    assert _saved(client) == {}
    response = _put(client, 11, [school.id])
    assert (response.status_code, response.json()["detail"]) == (404, "canvas_not_connected")


def test_demo_sandboxes_have_no_class_tags(demo_client, canvas):
    assert demo_client.get("/canvas/course-tags").json() == {"courses": []}
    assert _put(demo_client, 1001, []).status_code == 404


def test_a_new_token_for_the_same_account_keeps_class_tags(connected, db):
    school = _tag(db, "School")
    _put(connected, 11, [school.id])
    response = connected.put("/canvas/connection", json={"base_url": BASE_URL, "token": TOKEN})
    assert response.status_code == 200
    assert _saved(connected) == {11: ["School"]}


def test_another_canvas_account_resets_class_tags(connected, db, canvas):
    canvas.valid[OTHER_ACCOUNT_TOKEN] = {"id": 77, "name": "Grace Hopper"}
    school = _tag(db, "School")
    _put(connected, 11, [school.id])
    response = connected.put(
        "/canvas/connection",
        json={"base_url": BASE_URL, "token": OTHER_ACCOUNT_TOKEN, "allow_account_change": True},
    )
    assert response.status_code == 200, response.text
    assert _saved(connected) == {}


def test_removing_the_connection_removes_class_tags(connected, db):
    school = _tag(db, "School")
    _put(connected, 11, [school.id])
    connected.delete("/canvas/connection")
    assert _saved(connected) == {}
