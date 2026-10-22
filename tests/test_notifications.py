"""In-app notifications: each alert type's timing, quiet hours, preferences and read state."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.crud.notification_crud import get_notifications, prune_states, update_preferences
from app.models.habit_model import Habit
from app.models.mood_checkin_model import MoodCheckin
from app.models.notification_model import NotificationState
from app.models.profile_model import Profile
from app.models.task_model import Task
from tests.conftest import OTHER_USER_ID, USER_ID

UTC_TZ = ZoneInfo("UTC")
PACIFIC = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 20)  # A Tuesday.


def _utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _task(db, title, due: date, at: time | None = None, user_id=USER_ID, **fields) -> Task:
    task = Task(title=title, user_id=user_id, due_date=datetime.combine(due, time.min), due_time=at, **fields)
    db.add(task)
    db.flush()
    return task


def _profile(db, **fields) -> None:
    db.add(Profile(user_id=USER_ID, name="Test", **fields))
    db.flush()


def _mood(db, *days: date, kind="day", user_id=USER_ID) -> None:
    for day in days:
        db.add(MoodCheckin(user_id=user_id, period_kind=kind, period_start=day, mood=3))
    db.flush()


@pytest.fixture
def without_mood_alerts(db):
    """Mood alerts are on for every user by default; tests about other types switch them off."""
    update_preferences(db, USER_ID, {"types": {"mood_reminder": False, "mood_missed": False}})


def _ids(db, now: datetime, tz=UTC_TZ) -> list[str]:
    return [n.id for n in get_notifications(db, USER_ID, tz, now)]


def _by_id(db, now: datetime, tz=UTC_TZ) -> dict:
    return {n.id: n for n in get_notifications(db, USER_ID, tz, now)}


# ── Reminders and final warnings ─────────────────────────────────────────────

def test_reminder_then_final_warning_for_a_timed_task(db):
    task = _task(db, "Essay", DAY, time(16, 0))
    _task(db, "Done", DAY, time(16, 0), completed=True)
    _task(db, "Archived", DAY, time(16, 0), archived_at=datetime.now(UTC))
    _task(db, "Deleted", DAY, time(16, 0), deleted_at=datetime.now(UTC))
    _task(db, "Someone else's", DAY, time(16, 0), user_id=OTHER_USER_ID)
    reminder, warning = (f"{kind}:task-{task.id}:2026-10-20T16:00" for kind in ("task_reminder", "final_warning"))

    assert _ids(db, _utc(DAY, 13, 59)) == []
    current = _by_id(db, _utc(DAY, 14, 0))
    assert current[reminder].title == "Essay is due in 2 hours"
    assert current[reminder].body == "Due today at 4:00 PM."
    assert current[reminder].task_ids == [task.id]

    # The final warning takes over from the reminder.
    assert reminder in _ids(db, _utc(DAY, 15, 29))
    current = _by_id(db, _utc(DAY, 15, 30))
    assert reminder not in current
    assert current[warning].title == "Essay is due in 30 minutes"
    assert current[warning].fire_at == _utc(DAY, 15, 30)

    assert warning in _ids(db, _utc(DAY, 15, 59))
    assert _ids(db, _utc(DAY, 16, 0)) == []


def test_reminder_lead_comes_from_the_task_then_the_user_default(db):
    own = _task(db, "Own lead", DAY + timedelta(days=1), time(9, 0), reminder_minutes=24 * 60)
    default = _task(db, "Default lead", DAY, time(20, 0))
    update_preferences(db, USER_ID, {"default_reminder_minutes": 240})

    assert f"task_reminder:task-{own.id}:2026-10-21T09:00" not in _ids(db, _utc(DAY, 8, 59))
    current = _by_id(db, _utc(DAY, 9, 0))
    assert current[f"task_reminder:task-{own.id}:2026-10-21T09:00"].title == "Own lead is due in 1 day"
    assert current[f"task_reminder:task-{own.id}:2026-10-21T09:00"].body == "Due tomorrow at 9:00 AM."
    assert f"task_reminder:task-{default.id}:2026-10-20T20:00" not in _ids(db, _utc(DAY, 15, 59))
    assert f"task_reminder:task-{default.id}:2026-10-20T20:00" in _ids(db, _utc(DAY, 16, 0))


def test_a_lead_no_longer_than_the_final_warning_sends_only_the_warning(db):
    task = _task(db, "Quick", DAY, time(12, 0), reminder_minutes=30)
    assert _ids(db, _utc(DAY, 11, 45)) == [f"final_warning:task-{task.id}:2026-10-20T12:00"]


def test_reminder_lead_is_real_elapsed_time_across_dst(db):
    # 03:30 on 2026-03-08 is 10:30 UTC; two real hours before is 00:30 PST,
    # not the 01:30 a wall-clock subtraction would give.
    spring = date(2026, 3, 8)
    task = _task(db, "After the change", spring, time(3, 30))
    reminder = f"task_reminder:task-{task.id}:2026-03-08T03:30"
    assert reminder not in _ids(db, _utc(spring, 8, 29), PACIFIC)
    assert _by_id(db, _utc(spring, 8, 30), PACIFIC)[reminder].title == "After the change is due in 2 hours"


def test_moving_a_due_time_makes_a_new_occurrence(db):
    task = _task(db, "Essay", DAY, time(16, 0))
    assert f"task_reminder:task-{task.id}:2026-10-20T16:00" in _ids(db, _utc(DAY, 14, 0))
    task.due_time = time(18, 0)
    db.flush()
    current = _ids(db, _utc(DAY, 16, 0))
    assert f"task_reminder:task-{task.id}:2026-10-20T18:00" in current
    assert f"task_reminder:task-{task.id}:2026-10-20T16:00" not in current


# ── Retired types ────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("without_mood_alerts")
def test_due_today_overdue_and_habit_alerts_are_no_longer_sent(db):
    _task(db, "Date only", DAY)
    _task(db, "Late", DAY, time(6, 0))
    db.add(Habit(title="Read", user_id=USER_ID))
    db.flush()
    for hour in (8, 12, 21, 23):
        assert _ids(db, _utc(DAY, hour)) == []
    assert _ids(db, _utc(DAY + timedelta(days=1), 8)) == []


# ── Mood reminder ────────────────────────────────────────────────────────────

def test_mood_reminder_fires_an_hour_before_call_it_a_day_until_midnight(db):
    assert _ids(db, _utc(DAY, 20, 59)) == []
    reminder = _by_id(db, _utc(DAY, 21, 0))["mood_reminder:mood:2026-10-20"]
    assert reminder.type == "mood_reminder"
    assert reminder.title == "How was your day?"
    assert reminder.body == "Log your mood check-in before you call it a day at 10:00 PM."
    assert reminder.dates == [DAY]
    assert reminder.fire_at == _utc(DAY, 21, 0)
    assert _ids(db, _utc(DAY, 23, 59)) == ["mood_reminder:mood:2026-10-20"]
    assert _ids(db, _utc(DAY + timedelta(days=1), 0, 0)) == []


@pytest.mark.parametrize("lead, fires_at", [(30, time(19, 30)), (45, time(19, 15)), (150, time(17, 30))])
def test_mood_reminder_lead_is_any_number_of_minutes(db, lead, fires_at):
    _profile(db, shutoff_time="20:00")
    assert _ids(db, _utc(DAY, 19, 0)) == ["mood_reminder:mood:2026-10-20"]
    update_preferences(db, USER_ID, {"mood_reminder_minutes": lead})
    fire_at = datetime.combine(DAY, fires_at, tzinfo=UTC)
    assert _ids(db, fire_at - timedelta(minutes=1)) == []
    assert _by_id(db, fire_at)["mood_reminder:mood:2026-10-20"].fire_at == fire_at


def test_mood_reminder_lead_past_the_day_start_fires_at_the_day_start(db):
    _profile(db, day_start_time="08:00", shutoff_time="22:00")
    update_preferences(db, USER_ID, {"mood_reminder_minutes": 24 * 60 - 1})
    assert _ids(db, _utc(DAY, 7, 59)) == []
    reminder = _by_id(db, _utc(DAY, 8, 0))["mood_reminder:mood:2026-10-20"]
    assert reminder.fire_at == _utc(DAY, 8, 0)
    assert reminder.dates == [DAY]


def test_mood_reminder_skips_logged_days_rest_days_and_disabled_settings(db):
    _profile(db, rest_days=[3])  # Wednesday
    assert _ids(db, _utc(DAY + timedelta(days=1), 21, 0)) == []
    # A week or month check-in isn't the day's.
    _mood(db, date(2026, 10, 19), kind="week")
    _mood(db, DAY, user_id=OTHER_USER_ID)
    assert _ids(db, _utc(DAY, 21, 0)) == ["mood_reminder:mood:2026-10-20"]
    _mood(db, DAY)
    assert _ids(db, _utc(DAY, 21, 0)) == []

    profile = db.query(Profile).filter(Profile.user_id == USER_ID).one()
    profile.mood_checkins = {"daily": False}
    db.flush()
    assert _ids(db, _utc(DAY + timedelta(days=6), 21, 0)) == []


def test_mood_reminder_for_a_call_it_a_day_after_midnight(db):
    _profile(db, day_start_time="08:00", shutoff_time="01:00")
    tomorrow = DAY + timedelta(days=1)
    assert "mood_reminder:mood:2026-10-20" not in _ids(db, _utc(DAY, 23, 59))
    reminder = _by_id(db, _utc(tomorrow, 0, 0))["mood_reminder:mood:2026-10-20"]
    assert reminder.dates == [DAY]
    assert "mood_reminder:mood:2026-10-20" not in _ids(db, _utc(tomorrow, 1, 0))


def test_mood_reminder_follows_call_it_a_day_across_dst(db):
    # 22:00 Pacific is 05:00 UTC the next day before the 2026-11-01 fall
    # back, and 06:00 UTC after it.
    fall = date(2026, 11, 1)
    assert _ids(db, _utc(fall, 3, 59), PACIFIC) == []
    assert _ids(db, _utc(fall, 4, 0), PACIFIC) == ["mood_reminder:mood:2026-10-31"]
    assert "mood_reminder:mood:2026-11-01" not in _ids(db, _utc(fall + timedelta(days=1), 4, 59), PACIFIC)
    assert "mood_reminder:mood:2026-11-01" in _ids(db, _utc(fall + timedelta(days=1), 5, 0), PACIFIC)


def test_mood_alerts_can_be_switched_off(db):
    update_preferences(db, USER_ID, {"types": {"mood_reminder": False}})
    _mood(db, DAY - timedelta(days=3))
    assert _ids(db, _utc(DAY, 21, 0)) == ["mood_missed:mood:2026-10-20"]
    update_preferences(db, USER_ID, {"types": {"mood_missed": False}})
    assert _ids(db, _utc(DAY, 21, 0)) == []


# ── Missed mood check-ins ────────────────────────────────────────────────────

def test_mood_missed_lists_the_past_weeks_gaps_at_the_day_start_until_midnight(db):
    # Logged Oct 13, 15 and 18 of the seven days before Tue Oct 20.
    _mood(db, date(2026, 10, 13), date(2026, 10, 15), date(2026, 10, 18))
    _mood(db, date(2026, 10, 14), user_id=OTHER_USER_ID)

    assert _ids(db, _utc(DAY, 7, 59)) == []
    missed = _by_id(db, _utc(DAY, 8, 0))["mood_missed:mood:2026-10-20"]
    assert missed.title == "4 missed mood check-ins"
    assert missed.body == "No check-in for Oct 14, Oct 16, Oct 17 and Oct 19 yet."
    assert missed.dates == [date(2026, 10, 14), date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 19)]
    assert missed.fire_at == _utc(DAY, 8, 0)
    assert "mood_missed:mood:2026-10-20" in _ids(db, _utc(DAY, 23, 59))
    assert "mood_missed:mood:2026-10-20" not in _ids(db, _utc(DAY + timedelta(days=1), 7, 59))

    # Backfilling drops the day, and the last one drops the alert.
    _mood(db, date(2026, 10, 14), date(2026, 10, 16), date(2026, 10, 17))
    missed = _by_id(db, _utc(DAY, 8, 0))["mood_missed:mood:2026-10-20"]
    assert missed.title == "1 missed mood check-in"
    assert missed.body == "No check-in for Oct 19 yet."
    _mood(db, date(2026, 10, 19))
    assert "mood_missed:mood:2026-10-20" not in _ids(db, _utc(DAY, 8, 0))


def test_mood_missed_starts_at_the_first_check_in_and_skips_rest_days(db):
    # No daily check-in yet, so nothing is missed.
    _mood(db, date(2026, 10, 1), kind="month")
    assert _ids(db, _utc(DAY, 8, 0)) == []

    _profile(db, rest_days=[0, 6], day_start_time="07:00")  # Weekends.
    _mood(db, date(2026, 10, 15))
    missed = _by_id(db, _utc(DAY, 7, 0))["mood_missed:mood:2026-10-20"]
    assert missed.dates == [date(2026, 10, 16), date(2026, 10, 19)]
    # Not on a rest day itself.
    assert _ids(db, _utc(date(2026, 10, 24), 7, 0)) == []


def test_mood_missed_needs_daily_check_ins_on(db):
    _profile(db, mood_checkins={"daily": False, "weekly": True})
    _mood(db, DAY - timedelta(days=5))
    assert _ids(db, _utc(DAY, 8, 0)) == []


@pytest.mark.parametrize("remind", ["shutoff", "shutoff+30", "off"])
def test_a_stored_retired_remind_setting_is_ignored(db, remind):
    _profile(db, shutoff_time="20:00", mood_checkins={"remind": remind})
    assert _ids(db, _utc(DAY, 18, 59)) == []
    assert _by_id(db, _utc(DAY, 19, 0))["mood_reminder:mood:2026-10-20"].fire_at == _utc(DAY, 19, 0)
    update_preferences(db, USER_ID, {"mood_reminder_minutes": 30})
    assert _by_id(db, _utc(DAY, 19, 30))["mood_reminder:mood:2026-10-20"].fire_at == _utc(DAY, 19, 30)


# ── Quiet hours ──────────────────────────────────────────────────────────────

def test_quiet_hours_hold_alerts_until_they_end(db):
    update_preferences(db, USER_ID, {"quiet_hours_start": "22:00", "quiet_hours_end": "07:00"})
    _profile(db, day_start_time="06:00")
    task = _task(db, "Early", DAY, time(8, 0))

    assert _ids(db, _utc(DAY, 6, 59)) == []
    held = _by_id(db, _utc(DAY, 7, 0))[f"task_reminder:task-{task.id}:2026-10-20T08:00"]
    assert held.fire_at == _utc(DAY, 7, 0)
    # The countdown is from delivery, not from when it would have fired.
    assert held.title == "Early is due in 1 hour"


def test_alerts_held_past_the_point_they_apply_are_dropped(db):
    update_preferences(db, USER_ID, {"quiet_hours_start": "22:00", "quiet_hours_end": "07:00"})
    _task(db, "At seven", DAY, time(7, 0))
    # The reminder (05:00) and final warning (06:30) are held until 07:00,
    # when the task is already due.
    assert _ids(db, _utc(DAY, 7, 0)) == []


def test_quiet_hours_hold_and_drop_mood_alerts(db):
    update_preferences(db, USER_ID, {"quiet_hours_start": "20:30", "quiet_hours_end": "08:30"})
    _mood(db, DAY - timedelta(days=2))
    # The missed list waits for the quiet hours to end; the 21:00 reminder
    # would be held past midnight, when it lapses.
    assert _ids(db, _utc(DAY, 8, 29)) == []
    assert _by_id(db, _utc(DAY, 8, 30))["mood_missed:mood:2026-10-20"].fire_at == _utc(DAY, 8, 30)
    assert "mood_reminder:mood:2026-10-20" not in _ids(db, _utc(DAY, 23, 0))


def test_quiet_hours_within_one_day(db):
    update_preferences(db, USER_ID, {"quiet_hours_start": "12:00", "quiet_hours_end": "13:00"})
    task = _task(db, "Lunch", DAY, time(14, 0))
    reminder = f"task_reminder:task-{task.id}:2026-10-20T14:00"
    assert reminder not in _ids(db, _utc(DAY, 12, 59))
    assert _by_id(db, _utc(DAY, 13, 0))[reminder].fire_at == _utc(DAY, 13, 0)
    # Alerts outside quiet hours are untouched.
    assert _by_id(db, _utc(DAY, 13, 30))[f"final_warning:task-{task.id}:2026-10-20T14:00"].fire_at == _utc(
        DAY, 13, 30
    )


def test_quiet_hours_ending_in_the_repeated_hour_of_a_dst_fall_back(db):
    # US clocks fall back on 2026-11-01, so 01:00-02:00 happens twice. A
    # final warning at 01:45 PST (the second pass, 09:45 UTC) is held to
    # 01:50 PST (09:50 UTC), not to the already-past 01:50 PDT.
    fall = date(2026, 11, 1)
    update_preferences(db, USER_ID, {"quiet_hours_start": "00:00", "quiet_hours_end": "01:50"})
    task = _task(db, "Late night", fall, time(2, 15))
    warning = f"final_warning:task-{task.id}:2026-11-01T02:15"
    assert warning not in _ids(db, _utc(fall, 9, 49), PACIFIC)
    held = _by_id(db, _utc(fall, 9, 50), PACIFIC)[warning]
    assert held.fire_at == _utc(fall, 9, 50)
    assert held.title == "Late night is due in 25 minutes"


# ── Preferences ──────────────────────────────────────────────────────────────

def test_preferences_default_to_everything_on(client):
    resp = client.get("/get-notification-preferences")
    assert resp.status_code == 200
    assert resp.json() == {
        "types": {"task_reminder": True, "final_warning": True, "mood_reminder": True, "mood_missed": True},
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        "default_reminder_minutes": 120,
        "mood_reminder_minutes": 60,
    }


def test_switched_off_types_are_left_out(client, db):
    task = _task(db, "Essay", DAY, time(16, 0))
    resp = client.patch("/update-notification-preferences", json={"types": {"final_warning": False}})
    assert resp.status_code == 200
    resp = client.patch("/update-notification-preferences", json={"types": {"mood_missed": False}})
    assert resp.json()["types"]["final_warning"] is False
    assert resp.json()["types"]["mood_missed"] is False
    # Without the final warning, the reminder stays up until the task is due.
    assert _ids(db, _utc(DAY, 15, 45)) == [f"task_reminder:task-{task.id}:2026-10-20T16:00"]


def test_preference_updates_validate_and_reset(client):
    def patch(body):
        return client.patch("/update-notification-preferences", json=body)

    assert patch({"quiet_hours_start": "22:00"}).status_code == 400
    assert patch({"quiet_hours_start": "22:00", "quiet_hours_end": "22:00"}).status_code == 400
    assert patch({"quiet_hours_start": "25:00", "quiet_hours_end": "07:00"}).status_code == 422
    assert patch({"types": {"mood_checkin": False}}).status_code == 422
    assert patch({"default_reminder_minutes": 0}).status_code == 422
    assert patch({"mood_reminder_minutes": 0}).status_code == 422
    assert patch({"mood_reminder_minutes": 24 * 60}).status_code == 422
    assert patch({"mood_reminder_minutes": 24 * 60 - 1}).status_code == 200

    resp = patch({
        "quiet_hours_start": "22:00", "quiet_hours_end": "07:00", "default_reminder_minutes": 45,
        "mood_reminder_minutes": 45,
    })
    assert resp.json()["quiet_hours_start"] == "22:00"
    assert resp.json()["default_reminder_minutes"] == 45
    assert resp.json()["mood_reminder_minutes"] == 45
    # Fields left out are kept; nulls clear.
    resp = patch({"types": {"final_warning": False}})
    assert resp.json()["quiet_hours_end"] == "07:00"
    assert resp.json()["mood_reminder_minutes"] == 45
    resp = patch({
        "quiet_hours_start": None, "quiet_hours_end": None, "default_reminder_minutes": None,
        "mood_reminder_minutes": None,
    })
    assert resp.json()["quiet_hours_start"] is None
    assert resp.json()["default_reminder_minutes"] == 120
    assert resp.json()["mood_reminder_minutes"] == 60
    assert resp.json()["types"]["final_warning"] is False


def test_retired_types_are_accepted_and_ignored(client, db):
    resp = client.patch("/update-notification-preferences", json={"types": {"overdue": False, "due_today": True}})
    assert resp.status_code == 200
    assert set(resp.json()["types"]) == {"task_reminder", "final_warning", "mood_reminder", "mood_missed"}
    assert client.get("/get-notification-preferences").json() == resp.json()
    # Read and dismissed state from older clients is still taken.
    retired_ids = ("overdue:task-1:2026-10-20T16:00", "due_today:tasks:2026-10-20", "habit_reminder:habits:2026-10-20")
    for retired_id in retired_ids:
        assert client.post("/dismiss-notifications", json={"ids": [retired_id]}).status_code == 204


# ── Endpoints and read state ─────────────────────────────────────────────────

def _upcoming_task(db, title="Soon", user_id=USER_ID) -> Task:
    """A task an hour from the real current time, so its reminder is up, for tests through the API."""
    due = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    return _task(db, title, due.date(), due.time(), user_id=user_id)


def _get(client, tz="UTC"):
    resp = client.get("/get-notifications", params={"tz": tz})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.usefixtures("without_mood_alerts")
def test_get_notifications_returns_the_current_list(client, db):
    task = _upcoming_task(db)
    _upcoming_task(db, "Not mine", user_id=OTHER_USER_ID)
    [notification] = _get(client)
    assert notification["type"] == "task_reminder"
    assert notification["task_ids"] == [task.id]
    assert notification["fire_at"].endswith("Z")
    assert notification["read"] is False
    assert client.get("/get-notifications", params={"tz": "Mars/Base"}).status_code == 422


@pytest.mark.usefixtures("without_mood_alerts")
def test_mark_read_mark_all_read_and_dismiss(client, db):
    first, second, third = (_upcoming_task(db, title) for title in ("One", "Two", "Three"))
    ids = {n["task_ids"][0]: n["id"] for n in _get(client)}

    resp = client.post("/mark-notifications-read", json={"ids": [ids[first.id], ids[first.id]]})
    assert resp.status_code == 204
    read = {n["id"]: n["read"] for n in _get(client)}
    assert read == {ids[first.id]: True, ids[second.id]: False, ids[third.id]: False}

    assert client.post("/dismiss-notifications", json={"ids": [ids[second.id]]}).status_code == 204
    assert ids[second.id] not in [n["id"] for n in _get(client)]

    assert client.post("/mark-all-notifications-read", params={"tz": "UTC"}).status_code == 204
    assert [n["read"] for n in _get(client)] == [True, True]

    # Reading a dismissed notification again doesn't bring it back.
    client.post("/mark-notifications-read", json={"ids": [ids[second.id]]})
    assert ids[second.id] not in [n["id"] for n in _get(client)]


@pytest.mark.usefixtures("without_mood_alerts")
def test_read_state_is_per_user(client, db):
    _upcoming_task(db)
    [notification] = _get(client)
    db.add(NotificationState(
        user_id=OTHER_USER_ID, notification_id=notification["id"], occurs_on=date.today(),
        dismissed_at=datetime.now(UTC),
    ))
    db.flush()
    assert [n["id"] for n in _get(client)] == [notification["id"]]


@pytest.mark.parametrize("bad_id", [
    "final_warning",
    "final_warning:task-1",
    "made_up:task-1:2026-10-20",
    "final_warning:task-1:2026-13-40",
    "final_warning:task-1:2026-10-20T25:00",
    "final_warning:task-1:2026-10-20; DROP TABLE tasks",
])
def test_malformed_notification_ids_are_rejected(client, bad_id):
    assert client.post("/mark-notifications-read", json={"ids": [bad_id]}).status_code == 422
    assert client.post("/dismiss-notifications", json={"ids": [bad_id]}).status_code == 422


def test_state_for_old_occurrences_is_pruned_when_touched(client, db):
    today = datetime.now(UTC).date()
    old = today - timedelta(days=40)
    recent = today - timedelta(days=5)
    for user_id, day in ((USER_ID, old), (OTHER_USER_ID, old), (USER_ID, recent)):
        db.add(NotificationState(
            user_id=user_id, notification_id=f"mood_missed:mood:{day.isoformat()}", occurs_on=day,
            read_at=datetime.now(UTC),
        ))
    db.commit()
    assert client.post("/mark-notifications-read", json={"ids": []}).status_code == 204
    left = db.query(NotificationState.user_id, NotificationState.occurs_on).all()
    assert left == [(USER_ID, recent)]


def test_prune_keeps_a_day_of_slack_for_local_dates(db):
    edge = datetime.now(UTC).date() - timedelta(days=31)
    db.add(NotificationState(user_id=USER_ID, notification_id=f"mood_missed:mood:{edge}", occurs_on=edge))
    db.flush()
    prune_states(db)
    assert db.query(NotificationState).count() == 1


# ── Task reminder lead ───────────────────────────────────────────────────────

def test_tasks_accept_and_return_a_reminder_lead(client):
    resp = client.post("/create-task", json={"title": "Essay", "due_date": "2026-10-20", "reminder_minutes": 90})
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["reminder_minutes"] == 90

    resp = client.put(f"/update-task/{task['id']}", json={"title": "Essay", "reminder_minutes": 15})
    assert resp.json()["reminder_minutes"] == 15
    resp = client.put(f"/update-task/{task['id']}", json={"title": "Essay"})
    assert resp.json()["reminder_minutes"] is None
    assert [t["reminder_minutes"] for t in client.get("/get-tasks").json()] == [None]

    assert client.post("/create-task", json={"title": "Bad", "reminder_minutes": 0}).status_code == 422
    assert client.post("/create-task", json={"title": "Bad", "reminder_minutes": 7 * 24 * 60 + 1}).status_code == 422
