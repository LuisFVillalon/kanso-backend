"""Events: series CRUD, repeat rules, edit scopes, archive and trash, and how occurrences reach the calendar."""

from datetime import UTC, date, datetime, timedelta

from app.crud.account_crud import delete_user_rows
from app.crud.base import TRASH_RETENTION
from app.crud.trash_crud import purge_expired
from app.models.event_model import Event, EventException
from app.models.event_tag_model import event_tags
from tests.conftest import OTHER_USER_ID, USER_ID

PACIFIC = "America/Los_Angeles"
# A Sunday-to-Saturday week in October 2026.
SUN = date(2026, 10, 18)
MON, TUE, WED, THU, FRI, SAT = (SUN + timedelta(days=n) for n in range(1, 7))


def _create(client, **fields) -> dict:
    body = {
        "title": "CS 460 Lecture",
        "start_date": MON.isoformat(),
        "start_time": "10:00",
        "end_time": "10:50",
        "time_zone": PACIFIC,
        **fields,
    }
    resp = client.post("/create-event", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _occurrences(client, first: date, last: date, tz=PACIFIC) -> list[dict]:
    resp = client.get(
        "/get-event-occurrences", params={"start_date": first.isoformat(), "end_date": last.isoformat(), "tz": tz}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _days(occurrences: list[dict]) -> list[str]:
    return [o["date"] for o in occurrences]


# ── Creating ─────────────────────────────────────────────────────────────────

def test_create_returns_the_series_with_its_tags(client):
    event = _create(
        client,
        description="Room 201",
        tags=[{"name": "School", "color": "#8b5cf6"}, {"name": "CS", "color": "#3b82f6"}],
        repeat_days=[5, 1, 3, 3],
        end_date="2026-12-11",
        reminder_minutes=10,
    )
    assert event["repeat_days"] == [1, 3, 5]
    assert event["repeat_interval"] == 1
    assert event["end_date"] == "2026-12-11"
    assert {t["name"] for t in event["tags"]} == {"School", "CS"}
    assert [e["id"] for e in client.get("/get-events").json()] == [event["id"]]
    assert client.get(f"/get-event/{event['id']}").json()["title"] == "CS 460 Lecture"


def test_create_rejects_an_unworkable_series(client):
    base = {"title": "x", "start_date": MON.isoformat(), "time_zone": PACIFIC}
    for bad in (
        {"start_time": "10:00"},
        {"start_time": "10:00", "end_time": "10:00"},
        {"start_time": "10:00", "end_time": "11:00", "time_zone": "Mars/Olympus"},
        {"start_time": "10:00", "end_time": "11:00", "repeat_days": [1], "end_date": SUN.isoformat()},
        {"start_time": "10:00", "end_time": "11:00", "repeat_days": [7]},
        {"start_time": "10:00", "end_time": "11:00", "repeat_days": [1], "repeat_interval": 0},
        {"start_time": "10:00", "end_time": "11:00", "title": ""},
    ):
        assert client.post("/create-event", json={**base, **bad}).status_code == 422, bad


def test_all_day_events_drop_their_times_and_keep_their_day_in_every_zone(client):
    _create(client, title="MATH midterm", all_day=True, start_date=THU.isoformat())
    for tz in (PACIFIC, "Asia/Tokyo"):
        (occ,) = _occurrences(client, SUN, SAT, tz)
        assert (occ["date"], occ["end_date"]) == (THU.isoformat(), THU.isoformat())
        assert (occ["start_time"], occ["all_day"]) == (None, True)


def test_a_one_off_event_ignores_repeat_end_and_interval(client):
    event = _create(client, end_date=SUN.isoformat())
    assert event["end_date"] is None
    assert _days(_occurrences(client, SUN, SAT + timedelta(days=14))) == [MON.isoformat()]


def test_events_belong_to_their_owner(client, db):
    db.add(Event(user_id=OTHER_USER_ID, title="Theirs", start_date=MON, time_zone=PACIFIC, all_day=True,
                 repeat_days=[], repeat_interval=1))
    db.commit()
    theirs = db.query(Event).filter(Event.user_id == OTHER_USER_ID).one()
    assert client.get("/get-events").json() == []
    assert _occurrences(client, SUN, SAT) == []
    assert client.get(f"/get-event/{theirs.id}").status_code == 404
    assert client.put(f"/update-event/{theirs.id}", json={"title": "Mine"}).status_code == 404
    assert client.delete(f"/del-event/{theirs.id}").status_code == 404


# ── Repeat rules ─────────────────────────────────────────────────────────────

def test_weekly_on_chosen_days_between_start_and_end(client):
    _create(client, start_date=TUE.isoformat(), end_date=(MON + timedelta(days=7)).isoformat(), repeat_days=[1, 3, 5])
    days = _days(_occurrences(client, SUN, SAT + timedelta(days=14)))
    # Monday the 19th is before the start; Monday the 26th is the end date.
    assert days == ["2026-10-21", "2026-10-23", "2026-10-26"]


def test_every_n_weeks_counts_sunday_weeks_from_the_start(client):
    # Biweekly Tue/Thu starting on a Thursday: that week is "on", so its
    # Tuesday (before the start) is skipped, the next week is off.
    _create(client, start_date=THU.isoformat(), repeat_days=[2, 4], repeat_interval=2)
    days = _days(_occurrences(client, SUN, SUN + timedelta(days=41)))
    assert days == ["2026-10-22", "2026-11-03", "2026-11-05", "2026-11-17", "2026-11-19"]


def test_an_event_past_midnight_shows_on_both_days(client):
    _create(client, title="Night shift", start_date=FRI.isoformat(), start_time="22:00", end_time="02:00")
    (occ,) = _occurrences(client, FRI, FRI)
    assert (occ["date"], occ["start_time"], occ["end_date"], occ["end_time"]) == (
        "2026-10-23", "22:00:00", "2026-10-24", "02:00:00",
    )
    assert occ["starts_at"] == "2026-10-24T05:00:00Z"
    assert occ["ends_at"] == "2026-10-24T09:00:00Z"
    assert _days(_occurrences(client, SAT, SAT)) == ["2026-10-23"]
    assert _occurrences(client, SAT + timedelta(days=1), SAT + timedelta(days=1)) == []


def test_occurrences_keep_their_wall_clock_time_across_dst(client):
    # US Pacific falls back on Sun Nov 1 2026, 2:00 AM.
    _create(client, title="Brunch", start_date="2026-10-25", repeat_days=[0], start_time="10:00", end_time="11:00")
    _create(client, title="Halloween", start_date="2026-10-31", start_time="23:00", end_time="03:00")
    occurrences = _occurrences(client, date(2026, 10, 25), date(2026, 11, 1))
    brunches = [o for o in occurrences if o["title"] == "Brunch"]
    assert [(o["date"], o["start_time"], o["starts_at"]) for o in brunches] == [
        ("2026-10-25", "10:00:00", "2026-10-25T17:00:00Z"),
        ("2026-11-01", "10:00:00", "2026-11-01T18:00:00Z"),
    ]
    (night,) = [o for o in occurrences if o["title"] == "Halloween"]
    # 11 PM PDT to 3 AM PST is five hours.
    assert (night["starts_at"], night["ends_at"]) == ("2026-11-01T06:00:00Z", "2026-11-01T11:00:00Z")
    # A caller in UTC sees the same instants on its own clock.
    utc = _occurrences(client, date(2026, 11, 1), date(2026, 11, 1), "UTC")
    assert [(o["title"], o["start_time"]) for o in utc] == [("Halloween", "06:00:00"), ("Brunch", "18:00:00")]


def test_a_time_skipped_by_dst_moves_to_the_matching_instant_after(client):
    # US Pacific springs forward on Sun Mar 14 2027, 2:00 AM.
    _create(client, title="Early", start_date="2027-03-14", start_time="02:30", end_time="04:00")
    (occ,) = _occurrences(client, date(2027, 3, 14), date(2027, 3, 14))
    assert (occ["start_time"], occ["starts_at"]) == ("03:30:00", "2027-03-14T10:30:00Z")


# ── Edit scopes ──────────────────────────────────────────────────────────────

def _mwf(client, **fields) -> dict:
    return _create(client, start_date=MON.isoformat(), repeat_days=[1, 3, 5], **fields)


def _put(client, event_id, body, **params):
    return client.put(f"/update-event/{event_id}", params=params, json=body)


def test_this_event_edits_one_occurrence(client):
    event = _mwf(client)
    resp = _put(client, event["id"], {"title": "CS 460 Midterm", "start_time": "09:00"},
                scope="this", occurrence_date=WED.isoformat())
    assert resp.status_code == 200, resp.text
    week = _occurrences(client, SUN, SAT)
    assert [(o["date"], o["title"], o["start_time"], o["edited"]) for o in week] == [
        ("2026-10-19", "CS 460 Lecture", "10:00:00", False),
        ("2026-10-21", "CS 460 Midterm", "09:00:00", True),
        ("2026-10-23", "CS 460 Lecture", "10:00:00", False),
    ]
    assert week[1]["occurrence_date"] == "2026-10-21"


def test_editing_all_events_keeps_what_one_occurrence_changed(client):
    event = _mwf(client)
    _put(client, event["id"], {"title": "CS 460 Midterm"}, scope="this", occurrence_date=WED.isoformat())
    assert _put(client, event["id"], {"title": "CS 460", "start_time": "11:00", "end_time": "11:50"}).status_code == 200
    week = _occurrences(client, SUN, SAT)
    assert [(o["title"], o["start_time"]) for o in week] == [
        ("CS 460", "11:00:00"), ("CS 460 Midterm", "11:00:00"), ("CS 460", "11:00:00"),
    ]


def test_this_event_can_move_an_occurrence_to_another_day(client):
    event = _mwf(client)
    _put(client, event["id"], {"date": SAT.isoformat()}, scope="this", occurrence_date=FRI.isoformat())
    week = _occurrences(client, SUN, SAT)
    assert [(o["date"], o["occurrence_date"]) for o in week] == [
        ("2026-10-19", "2026-10-19"), ("2026-10-21", "2026-10-21"), ("2026-10-24", "2026-10-23"),
    ]
    # Moved out of a range, it leaves it, wherever the rule put it.
    assert _days(_occurrences(client, FRI, FRI)) == []


def test_this_event_can_give_an_occurrence_its_own_tags_and_clear_its_description(client):
    event = _mwf(client, description="Room 201", tags=[{"name": "School"}])
    _put(client, event["id"], {"tags": [{"name": "Exam"}], "description": None},
         scope="this", occurrence_date=WED.isoformat())
    by_day = {o["date"]: o for o in _occurrences(client, SUN, SAT)}
    assert [t["name"] for t in by_day["2026-10-21"]["tags"]] == ["Exam"]
    assert by_day["2026-10-21"]["description"] is None
    assert [t["name"] for t in by_day["2026-10-19"]["tags"]] == ["School"]
    assert by_day["2026-10-19"]["description"] == "Room 201"


def test_this_event_rejects_series_fields_and_needs_a_real_occurrence(client):
    event = _mwf(client)
    resp = _put(client, event["id"], {"repeat_days": [2]}, scope="this", occurrence_date=WED.isoformat())
    assert resp.status_code == 400
    assert "repeat_days" in resp.json()["detail"]
    assert _put(client, event["id"], {"title": "x"}, scope="this").status_code == 400
    assert _put(client, event["id"], {"title": "x"}, scope="this", occurrence_date=TUE.isoformat()).status_code == 404
    assert _put(client, event["id"], {"date": TUE.isoformat()}).status_code == 400
    assert _put(client, event["id"], {"title": None}).status_code == 422


def test_a_one_off_event_edits_the_series_whatever_the_scope(client):
    event = _create(client)
    resp = _put(client, event["id"], {"date": TUE.isoformat()}, scope="this", occurrence_date=MON.isoformat())
    assert resp.json()["start_date"] == TUE.isoformat()
    assert _days(_occurrences(client, SUN, SAT)) == [TUE.isoformat()]


def test_this_and_following_splits_the_series(client, db):
    event = _mwf(client, end_date="2026-11-30", tags=[{"name": "School"}])
    next_mon = MON + timedelta(days=7)
    _put(client, event["id"], {"title": "Moved review"}, scope="this", occurrence_date=next_mon.isoformat())
    resp = _put(client, event["id"], {"start_time": "13:00", "end_time": "13:50"},
                scope="following", occurrence_date=FRI.isoformat())
    assert resp.status_code == 200, resp.text
    rest = resp.json()
    assert rest["id"] != event["id"]
    assert (rest["split_from_id"], rest["start_date"], rest["end_date"]) == (event["id"], "2026-10-23", "2026-11-30")
    assert [t["name"] for t in rest["tags"]] == ["School"]
    assert client.get(f"/get-event/{event['id']}").json()["end_date"] == "2026-10-22"

    occurrences = _occurrences(client, SUN, next_mon)
    assert [(o["date"], o["event_id"], o["start_time"], o["title"]) for o in occurrences] == [
        ("2026-10-19", event["id"], "10:00:00", "CS 460 Lecture"),
        ("2026-10-21", event["id"], "10:00:00", "CS 460 Lecture"),
        ("2026-10-23", rest["id"], "13:00:00", "CS 460 Lecture"),
        # The exception on a later date moved over with the split.
        ("2026-10-26", rest["id"], "13:00:00", "Moved review"),
    ]
    assert db.query(EventException).filter(EventException.event_id == event["id"]).count() == 0


def test_following_from_the_first_occurrence_edits_all_events(client):
    event = _mwf(client)
    resp = _put(client, event["id"], {"title": "Renamed"}, scope="following", occurrence_date=MON.isoformat())
    assert resp.json()["id"] == event["id"]
    assert [e["title"] for e in client.get("/get-events").json()] == ["Renamed"]


def test_every_n_weeks_survives_a_split(client):
    event = _create(client, start_date=TUE.isoformat(), repeat_days=[2], repeat_interval=2)
    split_on = TUE + timedelta(days=28)
    _put(client, event["id"], {"title": "Later"}, scope="following", occurrence_date=split_on.isoformat())
    days = _days(_occurrences(client, SUN, SUN + timedelta(days=55)))
    assert days == ["2026-10-20", "2026-11-03", "2026-11-17", "2026-12-01"]


# ── Archive and visibility ───────────────────────────────────────────────────

def _daily_around_today(client, **fields) -> tuple[dict, date]:
    """A daily all-day series from a week ago to a week ahead, in UTC."""
    today = datetime.now(UTC).date()
    event = _create(
        client,
        title="Standup",
        all_day=True,
        start_date=(today - timedelta(days=7)).isoformat(),
        end_date=(today + timedelta(days=7)).isoformat(),
        repeat_days=list(range(7)),
        time_zone="UTC",
        **fields,
    )
    return event, today


def _flags(client, first, last) -> list[tuple]:
    return [(o["date"], o["past"], o["archived"]) for o in _occurrences(client, first, last, "UTC")]


def test_archived_series_keep_their_past_and_leave_the_future(client):
    event, today = _daily_around_today(client)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    assert _flags(client, yesterday, tomorrow) == [
        (yesterday.isoformat(), True, False), (today.isoformat(), False, False), (tomorrow.isoformat(), False, False),
    ]
    assert client.patch(f"/archive-event/{event['id']}").status_code == 200
    assert _flags(client, yesterday, tomorrow) == [(yesterday.isoformat(), True, True)]
    assert client.get("/get-events").json() == []
    assert [e["id"] for e in client.get("/get-events", params={"archived": True}).json()] == [event["id"]]

    client.patch(f"/unarchive-event/{event['id']}")
    assert len(_flags(client, yesterday, tomorrow)) == 3


def test_archiving_one_occurrence_lists_it_under_the_archive(client):
    event, today = _daily_around_today(client)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    for day in (yesterday, tomorrow):
        params = {"scope": "this", "occurrence_date": day.isoformat()}
        assert client.patch(f"/archive-event/{event['id']}", params=params).status_code == 200
    assert _flags(client, yesterday, tomorrow) == [
        (yesterday.isoformat(), True, True), (today.isoformat(), False, False),
    ]
    archived = client.get("/get-archived-event-occurrences", params={"tz": "UTC"}).json()
    assert [(o["date"], o["archived"]) for o in archived] == [
        (yesterday.isoformat(), True), (tomorrow.isoformat(), True),
    ]

    client.patch(f"/unarchive-event/{event['id']}", params={"scope": "this", "occurrence_date": tomorrow.isoformat()})
    assert len(_flags(client, yesterday, tomorrow)) == 3


def test_archive_this_and_following_archives_the_rest_of_the_series(client):
    event, today = _daily_around_today(client)
    resp = client.patch(
        f"/archive-event/{event['id']}", params={"scope": "following", "occurrence_date": today.isoformat()}
    )
    rest = resp.json()
    assert rest["archived_at"] is not None and rest["split_from_id"] == event["id"]
    flags = _flags(client, today - timedelta(days=1), today + timedelta(days=2))
    assert flags == [((today - timedelta(days=1)).isoformat(), True, False)]


def test_calendar_activity_includes_events_whatever_include_archived_says(client):
    event, today = _daily_around_today(client)
    client.patch(f"/archive-event/{event['id']}")
    for include_archived in (False, True):
        resp = client.get("/get-calendar-activity", params={
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "tz": "UTC",
            "include_archived": include_archived,
        })
        assert [(o["date"], o["archived"]) for o in resp.json()["events"]] == [
            ((today - timedelta(days=1)).isoformat(), True)
        ]


# ── Trash ────────────────────────────────────────────────────────────────────

def test_deleting_one_occurrence_trashes_just_it(client):
    event = _mwf(client)
    resp = client.delete(f"/del-event/{event['id']}", params={"scope": "this", "occurrence_date": WED.isoformat()})
    assert resp.status_code == 200
    assert _days(_occurrences(client, SUN, SAT)) == ["2026-10-19", "2026-10-23"]
    (item,) = client.get("/get-trash").json()
    assert (item["kind"], item["id"], item["occurrence_date"], item["title"]) == (
        "event", event["id"], "2026-10-21", "CS 460 Lecture",
    )

    assert client.patch(f"/restore-event/{event['id']}", params={"occurrence_date": WED.isoformat()}).status_code == 200
    assert len(_occurrences(client, SUN, SAT)) == 3
    assert client.get("/get-trash").json() == []


def test_purging_one_occurrence_keeps_it_gone(client):
    event = _mwf(client)
    client.delete(f"/del-event/{event['id']}", params={"scope": "this", "occurrence_date": WED.isoformat()})
    resp = client.delete(f"/purge-event/{event['id']}", params={"occurrence_date": WED.isoformat()})
    assert resp.status_code == 204
    assert client.get("/get-trash").json() == []
    assert _days(_occurrences(client, SUN, SAT)) == ["2026-10-19", "2026-10-23"]
    assert client.patch(f"/restore-event/{event['id']}", params={"occurrence_date": WED.isoformat()}).status_code == 404


def test_an_occurrence_past_retention_is_cancelled_for_good(client, db):
    event = _mwf(client)
    client.delete(f"/del-event/{event['id']}", params={"scope": "this", "occurrence_date": WED.isoformat()})
    exc = db.query(EventException).one()
    exc.deleted_at = datetime.now(UTC) - TRASH_RETENTION - timedelta(minutes=1)
    db.commit()
    purge_expired(db)
    db.commit()
    db.refresh(exc)
    assert (exc.deleted_at, exc.cancelled) == (None, True)
    assert len(_occurrences(client, SUN, SAT)) == 2


def test_deleting_all_events_trashes_the_series(client):
    event = _mwf(client)
    _put(client, event["id"], {"title": "Exam"}, scope="this", occurrence_date=WED.isoformat())
    client.delete(f"/del-event/{event['id']}")
    assert _occurrences(client, SUN, SAT) == []
    assert client.get("/get-events").json() == []
    assert [(i["kind"], i["id"], i["occurrence_date"]) for i in client.get("/get-trash").json()] == [
        ("event", event["id"], None)
    ]
    client.patch(f"/restore-event/{event['id']}")
    assert [o["title"] for o in _occurrences(client, SUN, SAT)] == ["CS 460 Lecture", "Exam", "CS 460 Lecture"]

    client.delete(f"/del-event/{event['id']}")
    assert client.delete(f"/purge-event/{event['id']}").status_code == 204
    assert client.get("/get-trash").json() == []


def test_deleting_this_and_following_trashes_the_split_off_part(client):
    event = _mwf(client)
    rest = client.delete(
        f"/del-event/{event['id']}", params={"scope": "following", "occurrence_date": WED.isoformat()}
    ).json()
    assert rest["deleted_at"] is not None
    assert _days(_occurrences(client, SUN, SAT + timedelta(days=7))) == ["2026-10-19"]
    assert [(i["id"], i["occurrence_date"]) for i in client.get("/get-trash").json()] == [(rest["id"], None)]
    client.patch(f"/restore-event/{rest['id']}")
    assert len(_occurrences(client, SUN, SAT)) == 3


def test_purging_a_series_keeps_the_part_split_off_it(client, db):
    event = _mwf(client)
    rest = _put(client, event["id"], {"title": "Later"}, scope="following", occurrence_date=WED.isoformat()).json()
    client.delete(f"/del-event/{event['id']}")
    assert client.delete(f"/purge-event/{event['id']}").status_code == 204
    assert client.get(f"/get-event/{rest['id']}").json()["split_from_id"] is None
    assert [o["title"] for o in _occurrences(client, SUN, SAT)] == ["Later", "Later"]


def test_unarchiving_following_on_an_active_series_changes_nothing(client):
    event = _mwf(client)
    resp = client.patch(
        f"/unarchive-event/{event['id']}", params={"scope": "following", "occurrence_date": WED.isoformat()}
    )
    assert resp.json()["id"] == event["id"]
    assert len(client.get("/get-events").json()) == 1


def test_deleting_the_account_removes_its_events(client, db):
    event = _mwf(client, tags=[{"name": "School"}])
    _put(client, event["id"], {"tags": [{"name": "Exam"}]}, scope="this", occurrence_date=WED.isoformat())
    delete_user_rows(db, USER_ID)
    db.commit()
    assert db.query(Event).count() == 0
    assert db.query(EventException).count() == 0
    assert db.execute(event_tags.select()).all() == []
