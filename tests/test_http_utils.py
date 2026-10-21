"""The shared request validators in core/http_utils, and the error bodies the routes using them return."""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.core.http_utils import parse_iso_date, require_date_range

INVALID_DATE = "Invalid date format. Use YYYY-MM-DD."
REVERSED_RANGE = "start_date must not be after end_date."


def test_parse_iso_date():
    assert parse_iso_date("2026-10-21") == date(2026, 10, 21)
    for value in ("", "10/21/2026", "2026-13-01", "not-a-date"):
        with pytest.raises(HTTPException) as exc:
            parse_iso_date(value)
        assert (exc.value.status_code, exc.value.detail) == (400, INVALID_DATE)


def test_require_date_range():
    day = date(2026, 10, 21)
    require_date_range(day, day)
    require_date_range(day, day, max_days=1)
    require_date_range(day, day + timedelta(days=365), max_days=366)
    require_date_range(day, day + timedelta(days=1000))  # no limit without max_days

    with pytest.raises(HTTPException) as exc:
        require_date_range(day, day - timedelta(days=1))
    assert (exc.value.status_code, exc.value.detail) == (400, REVERSED_RANGE)

    with pytest.raises(HTTPException) as exc:
        require_date_range(day, day + timedelta(days=366), max_days=366)
    assert (exc.value.status_code, exc.value.detail) == (400, "The range may span at most 366 days.")


def test_note_time_range_errors(client):
    def detail(start, end):
        resp = client.get("/note-session/totals-range", params={"start_date": start, "end_date": end})
        assert resp.status_code == 400
        return resp.json()["detail"]

    assert detail("2026-10-21", "nope") == INVALID_DATE
    assert detail("nope", "2026-10-21") == INVALID_DATE
    assert detail("2026-10-21", "2026-10-20") == REVERSED_RANGE


def test_toggle_habit_date_errors(client):
    habit = client.post("/create-habit", json={"title": "Stretch"}).json()

    def toggle(**body):
        return client.patch(f"/toggle-habit-date/{habit['id']}", json=body)

    resp = toggle(date="21/10/2026")
    assert (resp.status_code, resp.json()["detail"]) == (400, INVALID_DATE)

    ahead = date.today() + timedelta(days=2)
    resp = toggle(date=ahead.isoformat(), local_date="garbage")
    assert (resp.status_code, resp.json()["detail"]) == (400, "Cannot log future dates.")
    # A caller already a day ahead of the server may log its own today.
    assert toggle(date=ahead.isoformat(), local_date=ahead.isoformat()).status_code == 200
