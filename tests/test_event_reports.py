"""Events in Reports (GET /get-event-report) and the daily brief: time by tag and day, and free work-window time."""

from datetime import UTC, date, datetime, time, timedelta

from app.models.profile_model import Profile
from app.models.task_model import Task
from tests.conftest import USER_ID

PACIFIC = "America/Los_Angeles"
# A Sunday-to-Saturday week, long past, so every occurrence in it has ended.
SUN, MON, TUE, WED, THU, FRI, SAT = (date(2026, 9, 13 + n) for n in range(7))


def _profile(db, **fields) -> None:
    db.add(Profile(user_id=USER_ID, name="Test", rest_days=[0, 6], day_start_time="08:00", shutoff_time="22:00",
                   **fields))
    db.flush()


def _event(client, title, day: date, start=None, end=None, tags=(), **fields) -> dict:
    body = {
        "title": title,
        "start_date": day.isoformat(),
        "time_zone": PACIFIC,
        "tags": [{"name": t} for t in tags],
        **({"start_time": start, "end_time": end} if start else {"all_day": True}),
        **fields,
    }
    resp = client.post("/create-event", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _report(client, first=SUN, last=SAT) -> dict:
    resp = client.get(
        "/get-event-report", params={"start_date": first.isoformat(), "end_date": last.isoformat(), "tz": PACIFIC}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _week_of_events(client, db):
    _profile(db)
    _event(client, "CS 460", MON, "10:00", "11:00", ["School"], repeat_days=[1, 3, 5], end_date=FRI.isoformat())
    _event(client, "Advising", MON, "10:30", "12:00", ["School", "Work"])
    _event(client, "Gym", TUE, "07:00", "09:00", ["Health"])
    _event(client, "Night shift", FRI, "23:00", "01:00")
    _event(client, "Midterm", THU)
    cancelled = _event(client, "Office hours", WED, "13:00", "14:00", ["School"])
    client.patch(f"/archive-event/{cancelled['id']}")
    _event(client, "Farmers market", SUN, "12:00", "13:00")


def test_report_counts_event_time_by_day_and_the_free_time_left_in_the_work_windows(client, db):
    _week_of_events(client, db)
    report = _report(client)
    days = {d["date"]: d for d in report["by_day"]}
    summary = [
        (d["count"], d["minutes"], d["is_rest_day"], d["window_minutes"], d["event_window_minutes"], d["free_minutes"])
        for d in (days[day.isoformat()] for day in (SUN, MON, TUE, WED, THU, FRI, SAT))
    ]
    assert summary == [
        (1, 60, True, 0, 0, 0),
        # Overlapping events take the window once: 10:00 to 12:00.
        (2, 150, False, 840, 120, 720),
        # Only 8:00 to 9:00 of a 7:00 gym session is in the window.
        (1, 120, False, 840, 60, 780),
        # The archived office hours are past, so they still count.
        (2, 120, False, 840, 120, 720),
        # All-day events count but take no time.
        (1, 0, False, 840, 0, 840),
        # The night shift is outside the window and runs into Saturday.
        (2, 120, False, 840, 60, 780),
        (0, 60, True, 0, 0, 0),
    ]
    assert (report["event_count"], report["archived_count"], report["total_minutes"]) == (9, 1, 630)
    assert (report["window_minutes"], report["event_window_minutes"], report["free_minutes"]) == (4200, 360, 3840)
    assert report["free_pct"] == 91.4
    assert (report["busiest_day"]["date"], report["busiest_day"]["minutes"]) == (MON.isoformat(), 150)
    assert [(b["tag"]["name"] if b["tag"] else None, b["minutes"], b["count"]) for b in report["by_tag"]] == [
        ("School", 330, 5), (None, 180, 3), ("Health", 120, 1), ("Work", 90, 1),
    ]
    assert report["by_week"] == [{"start": SUN.isoformat(), "count": 9, "minutes": 630}]
    assert report["by_month"] == [{"start": "2026-09-01", "count": 9, "minutes": 630}]


def test_report_leaves_out_upcoming_archived_events(client, db):
    _profile(db)
    soon = datetime.now(UTC).date() + timedelta(days=30)
    event = _event(client, "Dropped class", soon, "10:00", "11:00")
    client.patch(f"/archive-event/{event['id']}")
    report = _report(client, soon, soon)
    assert (report["event_count"], report["total_minutes"]) == (0, 0)


def test_report_without_events_has_all_its_window_free(client, db):
    _profile(db)
    report = _report(client)
    assert (report["event_count"], report["free_minutes"], report["free_pct"], report["busiest_day"]) == (
        0, 4200, 100.0, None,
    )


# ── Daily brief ──────────────────────────────────────────────────────────────

def _brief(client, **params) -> dict:
    resp = client.get("/daily-debrief", params={"local_date": TUE.isoformat(), "local_time": "12:00", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _todays_events(client, db):
    _profile(db)
    _event(client, "Gym", TUE, "07:00", "08:00", ["Health"])
    _event(client, "Lecture", TUE, "09:00", "10:30", ["School"])
    _event(client, "Study group", TUE, "10:00", "11:00", ["School"])
    _event(client, "Library shift", TUE, "13:00", "17:00", ["Work"])
    cancelled = _event(client, "Cancelled lab", TUE, "15:00", "16:00", ["School"])
    client.patch(f"/archive-event/{cancelled['id']}")
    _event(client, "Career fair", TUE)
    db.add(Task(title="Essay", user_id=USER_ID, due_date=datetime.combine(TUE, time.min), estimated_time=2))
    db.commit()


def test_event_time_in_the_work_window_comes_out_of_todays_capacity(client, db):
    _todays_events(client, db)
    workload = _brief(client, tz=PACIFIC)["workload"]
    # 9:00 to 11:00 and 13:00 to 17:00; the gym is before the window opens
    # and the archived lab isn't happening.
    assert (workload["window_minutes"], workload["event_minutes"], workload["available_minutes"]) == (840, 360, 480)
    assert (workload["committed_minutes"], workload["free_minutes"], workload["utilization_pct"]) == (120, 360, 25.0)
    assert workload["is_overcommitted"] is False


def test_the_brief_lists_todays_events_and_the_ones_that_have_passed(client, db):
    _todays_events(client, db)
    brief = _brief(client, tz=PACIFIC)
    assert [(e["title"], e["archived"]) for e in brief["events_today"]] == [
        ("Career fair", False), ("Gym", False), ("Lecture", False), ("Study group", False),
        ("Library shift", False), ("Cancelled lab", True),
    ]
    assert [e["title"] for e in brief["passed_events_today"]] == ["Gym", "Lecture", "Study group"]

    def by_tag(buckets):
        return [(b["tag"]["name"] if b["tag"] else None, b["minutes"], b["count"]) for b in buckets]

    assert by_tag(brief["event_minutes_by_tag"]) == [
        ("Work", 240, 1), ("School", 150, 2), ("Health", 60, 1), (None, 0, 1),
    ]
    assert by_tag(brief["passed_event_minutes_by_tag"]) == [("School", 150, 2), ("Health", 60, 1)]


def test_the_brief_reads_events_in_their_own_zone_without_tz(client, db):
    _todays_events(client, db)
    assert _brief(client)["workload"]["event_minutes"] == 360
    assert _brief(client, local_time=None)["passed_events_today"] == []


def test_events_filling_the_window_leave_no_capacity(client, db):
    _profile(db, day_windows={"2": {"start": "09:00", "end": "11:00"}})
    _event(client, "Lecture", TUE, "09:00", "10:30")
    _event(client, "Study group", TUE, "10:00", "12:00")
    db.add(Task(title="Essay", user_id=USER_ID, due_date=datetime.combine(TUE, time.min), estimated_time=1))
    db.commit()
    workload = _brief(client, tz=PACIFIC)["workload"]
    assert (workload["window_minutes"], workload["event_minutes"], workload["available_minutes"]) == (120, 120, 0)
    assert (workload["utilization_pct"], workload["is_overcommitted"], workload["free_minutes"]) == (None, True, 0)


def test_the_brief_rejects_an_unknown_time_zone(client):
    resp = client.get("/daily-debrief", params={"local_date": TUE.isoformat(), "tz": "Mars/Olympus"})
    assert resp.status_code == 422
