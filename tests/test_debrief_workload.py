"""Daily debrief workload: available hours follow the day's work window."""

from datetime import date, datetime, time

from app.models.profile_model import Profile
from app.models.task_model import Task
from tests.conftest import USER_ID

MONDAY, TUESDAY = date(2026, 10, 19), date(2026, 10, 20)


def _workload(client, day: date) -> dict:
    resp = client.get("/daily-debrief", params={"local_date": day.isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.json()["workload"]


def test_available_hours_use_the_weekdays_window_and_fall_back_to_the_daily_times(client, db):
    db.add(Profile(
        user_id=USER_ID, name="Test", day_start_time="08:00", shutoff_time="22:00",
        day_windows={"1": {"start": "09:00", "end": "13:00"}},
    ))
    for day in (MONDAY, TUESDAY):
        db.add(Task(title="Five hours", user_id=USER_ID, due_date=datetime.combine(day, time.min), estimated_time=5))
    db.commit()

    monday = _workload(client, MONDAY)
    assert monday["available_minutes"] == 4 * 60
    assert monday["is_overcommitted"] is True

    tuesday = _workload(client, TUESDAY)
    assert tuesday["available_minutes"] == 14 * 60
    assert tuesday["is_overcommitted"] is False


def test_a_weekdays_window_counts_without_daily_times(client, db):
    db.add(Profile(user_id=USER_ID, name="Test", day_windows={"1": {"start": "09:00", "end": "17:30"}}))
    db.commit()
    assert _workload(client, MONDAY)["available_minutes"] == 8.5 * 60
    assert _workload(client, TUESDAY)["available_minutes"] is None
