"""Profile: the home_panels preference."""

import pytest

from app.models.profile_model import Profile
from tests.conftest import USER_ID


def test_home_panels_defaults_to_null(client, db):
    db.add(Profile(user_id=USER_ID, name="Test"))
    db.flush()
    assert client.get("/get-profile").json()["home_panels"] is None


@pytest.mark.parametrize(
    "value",
    ["tasks", "notes", "canvas", "tasks,notes", "tasks,canvas", "notes,canvas", "tasks,notes,canvas", "canvas,tasks"],
)
def test_home_panels_round_trips(client, value):
    resp = client.post("/save-profile", json={"name": "Test", "home_panels": value})
    assert resp.status_code == 200
    assert resp.json()["home_panels"] == value
    assert client.get("/get-profile").json()["home_panels"] == value

    # Saving other fields leaves it alone; null clears it.
    assert client.post("/save-profile", json={"name": "Renamed"}).json()["home_panels"] == value
    assert client.post("/save-profile", json={"name": "Test", "home_panels": None}).json()["home_panels"] is None


@pytest.mark.parametrize("value", ["notes,tasks", "calendar", "tasks,tasks", "canvas,notes", "", "Tasks,Notes", 1])
def test_home_panels_rejects_other_values(client, value):
    resp = client.post("/save-profile", json={"name": "Test", "home_panels": value})
    assert resp.status_code == 422
