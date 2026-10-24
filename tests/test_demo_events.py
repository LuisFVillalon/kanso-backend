"""The demo's calendar events: the mix of series and one-offs, and how they expand onto the calendar."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.core.auth import UserInfo, get_current_user
from app.crud.demo_crud import _TAGS, DEFAULT_TIME_ZONE, HISTORY_DAYS, seed_demo_data
from app.crud.event_crud import get_events
from app.crud.event_occurrence_crud import (
    get_archived_occurrences,
    get_event_report,
    get_occurrences,
    occurrences_between,
    overlap_minutes,
    todays_events,
    window_span,
)
from app.crud.profile_crud import get_profile, work_window
from app.database.database import get_db
from app.main import app
from app.models.calendar_settings_model import CalendarSettings
from app.models.event_model import Event
from tests.conftest import USER_ID

TODAY = date(2026, 10, 20)  # A Tuesday.
ZONE = ZoneInfo(DEFAULT_TIME_ZONE)
CLASS_TAGS = {"CS 460", "MATH 254", "PSY 101", "COMM 103", "ART 157"}


def _seed(db, **kwargs):
    seed_demo_data(db, USER_ID, local_date=TODAY.isoformat(), **kwargs)


def _series(db) -> dict[str, Event]:
    return {e.title: e for e in db.query(Event).filter(Event.user_id == USER_ID)}


def _weekday(day: date) -> int:
    return day.isoweekday() % 7  # 0=Sun..6=Sat, as Event.repeat_days


def test_the_seed_has_every_kind_of_event(db):
    _seed(db)
    events = list(_series(db).values())
    repeating = [e for e in events if e.repeat_days]
    one_offs = [e for e in events if not e.repeat_days]

    assert _series(db)["CS 460 Lecture"].repeat_days == [1, 3]
    assert _series(db)["PSY 101 Lecture"].repeat_days == [2, 4]
    assert any(e.repeat_interval == 2 for e in repeating)
    assert any(e.repeat_days == [6] and e.repeat_interval == 1 for e in repeating)  # The weekly gym session.
    assert any(e.end_date is not None for e in repeating) and any(e.end_date is None for e in repeating)
    assert any(e.all_day for e in one_offs)
    assert any(not e.all_day and e.end_time < e.start_time for e in one_offs)  # Runs past midnight.
    archived = [e.start_date for e in events if e.archived_at is not None]
    assert any(d < TODAY for d in archived) and any(d > TODAY for d in archived)
    assert any(e.reminder_minutes for e in events) and any(e.reminder_minutes is None for e in events)

    exceptions = [exc for e in events for exc in e.exceptions]
    assert any(exc.cancelled for exc in exceptions)
    assert any(exc.title and not exc.cancelled for exc in exceptions)

    tag_names = {t.name for e in events for t in e.tags}
    assert tag_names <= set(_TAGS)
    assert CLASS_TAGS | {"Work", "Health", "Personal"} <= tag_names
    assert all(e.time_zone == DEFAULT_TIME_ZONE for e in events)


def test_classes_end_with_the_term_and_open_series_go_on(db):
    _seed(db)
    term_end = db.query(CalendarSettings).filter(CalendarSettings.user_id == USER_ID).one().end_date
    series = _series(db)
    for title in ("CS 460 Lecture", "PSY 101 Lecture", "ART 157 Studio"):
        assert series[title].end_date == term_end
    after = occurrences_between(db, USER_ID, term_end + timedelta(days=1), term_end + timedelta(days=21), ZONE)
    titles = {occ.title for occ in after}
    assert not titles & {"CS 460 Lecture", "MATH 254 Lecture", "COMM 103 Lecture", "PSY 101 Lecture"}
    assert {"Gym session", "Library desk shift", "Book club"} <= titles


def test_occurrences_cover_the_history_and_three_weeks_ahead(db):
    _seed(db)
    first, last = TODAY - timedelta(days=HISTORY_DAYS), TODAY + timedelta(days=21)
    occurrences = occurrences_between(db, USER_ID, first, last, ZONE)
    days = {occ.day for occ in occurrences}
    assert min(days) < first + timedelta(days=7)
    assert max(days) > last - timedelta(days=7)
    # Every week of the window has something on the calendar.
    assert all(any(d <= day < d + timedelta(days=7) for day in days) for d in
               (first + timedelta(weeks=n) for n in range((last - first).days // 7)))

    by_title: dict[str, list] = {}
    for occ in occurrences:
        by_title.setdefault(occ.title, []).append(occ)
    lectures = by_title["CS 460 Lecture"]
    assert {_weekday(occ.day) for occ in lectures} == {1, 3}
    assert all((occ.start_time, occ.end_time) == (time(10, 0), time(11, 15)) for occ in lectures)
    cancelled = next(exc for exc in _series(db)["CS 460 Lecture"].exceptions if exc.cancelled)
    assert cancelled.occurrence_date < TODAY
    assert cancelled.occurrence_date not in {occ.day for occ in lectures}

    studio = sorted(occ.day for occ in by_title["ART 157 Studio"])
    assert len(studio) >= 4
    assert {(b - a).days for a, b in zip(studio, studio[1:], strict=False)} == {14}

    midterm = by_title["PSY 101 Midterm"]
    assert len(midterm) == 1 and midterm[0].day > TODAY and midterm[0].edited
    assert midterm[0].end_time == time(14, 30)
    assert "PSY 101 Lecture" in by_title

    hackathon = by_title["CS 460 Hackathon"][0]
    assert hackathon.ends_at.astimezone(ZONE).date() == hackathon.day + timedelta(days=1)
    assert by_title["Campus career fair"][0].all_day


def test_archived_events_leave_the_upcoming_calendar(db):
    _seed(db)
    now = datetime.now(UTC)
    shown = get_occurrences(db, USER_ID, TODAY - timedelta(days=HISTORY_DAYS), TODAY + timedelta(days=21), ZONE, now)
    titles = {occ.title: occ for occ in shown}
    assert titles["COMM 103 Speech practice"].archived  # Past, so kept as history.
    assert "Dentist appointment" not in titles
    assert {e.title for e in get_events(db, USER_ID, archived=True)} == {
        "COMM 103 Speech practice", "Dentist appointment"
    }
    assert get_archived_occurrences(db, USER_ID, ZONE, now) == []


def test_events_sit_mostly_inside_the_work_windows(db):
    """So the brief's capacity and Reports › Events have time to count."""
    _seed(db)
    profile = get_profile(db, USER_ID)
    total = inside = 0.0
    for occ in occurrences_between(db, USER_ID, TODAY - timedelta(days=HISTORY_DAYS), TODAY, ZONE):
        if occ.all_day:
            continue
        total += occ.minutes()
        if _weekday(occ.day) not in profile.rest_days:
            inside += overlap_minutes(occ.starts_at, occ.ends_at, *window_span(profile, occ.day, ZONE))
    assert inside / total > 0.9

    report = get_event_report(db, USER_ID, TODAY - timedelta(days=30), TODAY - timedelta(days=1), ZONE,
                              datetime.now(UTC))
    assert {b.tag.name for b in report.by_tag if b.minutes > 0} >= CLASS_TAGS | {"Work", "Health"}
    assert report.event_window_minutes > 0 and report.free_pct < 100

    today = todays_events(db, USER_ID, TODAY, time(8, 0), ZONE, work_window(profile, TODAY))
    assert {occ.title for occ in today.shown} >= {"COMM 103 Lecture", "PSY 101 Lecture", "Library desk shift"}
    assert today.window_minutes_taken > 0


def test_seeding_twice_gives_the_same_events(db):
    def snapshot():
        return sorted(
            (e.title, e.start_date, e.end_date, e.start_time, e.end_time, tuple(e.repeat_days), e.repeat_interval,
             tuple(sorted((x.occurrence_date, x.cancelled, x.title) for x in e.exceptions)))
            for e in _series(db).values()
        )

    _seed(db)
    first = snapshot()
    _seed(db)
    assert snapshot() == first


def test_the_seed_endpoint_puts_events_in_the_callers_zone(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=USER_ID, email="", provider="anonymous", is_anonymous=True
    )
    try:
        client = TestClient(app)
        params = {"local_date": TODAY.isoformat()}
        assert client.post("/demo/seed", params={**params, "tz": "Mars/Olympus"}).status_code == 422
        assert client.post("/demo/seed", params={**params, "tz": "America/New_York"}).status_code == 204
        assert {e.time_zone for e in _series(db).values()} == {"America/New_York"}
        assert client.post("/demo/seed", params=params).status_code == 204
        assert {e.time_zone for e in _series(db).values()} == {DEFAULT_TIME_ZONE}
    finally:
        app.dependency_overrides.clear()
