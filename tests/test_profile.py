"""Profile: the home_panels preference and per-weekday work windows."""

from datetime import date

import pytest

from app.crud.demo_crud import seed_demo_data
from app.crud.profile_crud import day_window, get_profile
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


# ── Per-weekday work windows ─────────────────────────────────────────────────

WINDOWS = {"1": {"start": "09:00", "end": "17:00"}, "5": {"start": "17:00", "end": "09:00"}}


def test_day_windows_round_trip(client):
    resp = client.post("/save-profile", json={"name": "Test", "day_windows": WINDOWS})
    assert resp.status_code == 200, resp.text
    assert resp.json()["day_windows"] == WINDOWS
    assert client.get("/get-profile").json()["day_windows"] == WINDOWS

    # Saving other fields leaves them alone; a new object replaces them; null clears them.
    assert client.post("/save-profile", json={"name": "Renamed"}).json()["day_windows"] == WINDOWS
    sunday = {"0": {"start": "10:00", "end": "14:00"}}
    assert client.post("/save-profile", json={"name": "Test", "day_windows": sunday}).json()["day_windows"] == sunday
    assert client.post("/save-profile", json={"name": "Test", "day_windows": None}).json()["day_windows"] is None


@pytest.mark.parametrize(
    "windows",
    [
        {"7": {"start": "09:00", "end": "17:00"}},
        {"mon": {"start": "09:00", "end": "17:00"}},
        {"1": {"start": "9:00", "end": "17:00"}},
        {"1": {"start": "09:00", "end": "24:00"}},
        {"1": {"start": "09:60", "end": "17:00"}},
        {"1": {"start": "09:00", "end": "09:00"}},
        {"1": {"start": "09:00"}},
        {"1": None},
        ["09:00", "17:00"],
    ],
)
def test_day_windows_rejects_invalid_windows(client, windows):
    resp = client.post("/save-profile", json={"name": "Test", "day_windows": windows})
    assert resp.status_code == 422


def test_day_window_uses_the_weekdays_entry_and_falls_back_to_the_daily_times(db):
    monday, tuesday = date(2026, 10, 19), date(2026, 10, 20)
    assert day_window(None, monday) == (None, None)

    db.add(Profile(user_id=USER_ID, name="Test", day_start_time="08:00", shutoff_time="22:00"))
    db.flush()
    profile = get_profile(db, USER_ID)
    assert day_window(profile, monday) == ("08:00", "22:00")

    profile.day_windows = WINDOWS
    assert day_window(profile, monday) == ("09:00", "17:00")
    assert day_window(profile, tuesday) == ("08:00", "22:00")


def test_demo_profile_has_valid_day_windows_and_a_rest_day(client, db):
    seed_demo_data(db, USER_ID, local_date="2026-10-20")
    profile = get_profile(db, USER_ID)
    assert profile.day_windows and profile.rest_days
    # No window on a rest day, and the saved profile reads back through the API's schema.
    assert not set(profile.day_windows) & {str(d) for d in profile.rest_days}
    assert day_window(profile, date(2026, 10, 19)) == ("08:00", "21:00")  # A Monday.
    assert client.get("/get-profile").json()["day_windows"] == profile.day_windows
