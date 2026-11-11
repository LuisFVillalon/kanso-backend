"""Kanso-only edits, tags, archive, delete and busy time of events imported from Google."""

# The Google fixtures come from test_google_calendar, and tests take them as arguments.
# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

from app.crud import account_crud
from app.crud.trash_crud import purge_expired
from app.models.google_calendar_model import GoogleCalendarEvent, GoogleEventOverride
from app.models.profile_model import Profile
from tests.conftest import USER_ID
from tests.test_google_calendar import (  # noqa: F401  (fixtures)
    PRIMARY,
    TZ,
    _activity_events,
    _as_other_user,
    _at,
    _connect,
    _day,
    _resync,
    _row,
    _tag,
    _timed,
    configured,
    fresh_rate_limit,
    google,
    key_env,
)

LONG_AGO = "2000-01-01T00:00:00Z"


def _instance(n: int, **extra) -> dict:
    """The nth weekly instance of the repeating Google event "r"."""
    return _timed(
        f"r_{n}", _at(7 * n, "10:00"), _at(7 * n, "11:00"),
        recurringEventId="r", summary="Standup", updated=LONG_AGO, **extra,
    )


def _id(db, google_event_id: str) -> int:
    return -_row(db, google_event_id).id


def _on(client, n: int) -> list[dict]:
    return _activity_events(client, _day(7 * n))


def _title(client, n: int) -> str | None:
    events = _on(client, n)
    return events[0]["title"] if events else None


def _tags(client, n: int) -> list[str]:
    return [t["name"] for t in _on(client, n)[0]["tags"]]


def _series(client, db, google, count=3) -> None:
    google.full[PRIMARY] = [_instance(n) for n in range(count)]
    _connect(client)


def _import_later(db, google, *items) -> None:
    google.changes[PRIMARY] = list(items)
    _resync(db)


# ── Shape ────────────────────────────────────────────────────────────────────

def test_repeating_instances_say_so(client, db, google):
    google.full[PRIMARY] = [_instance(0), _timed("one")]
    _connect(client)
    by_title = {e["title"]: e for e in _on(client, 0)}
    assert by_title["Standup"]["repeating"] is True
    assert by_title["Event one"]["repeating"] is False
    assert all(e["source"] == "google" and e["read_only"] is False for e in by_title.values())


def test_get_event_returns_an_imported_event_like_a_series(client, db, google):
    _series(client, db, google, 1)
    response = client.get(f"/get-event/{_id(db, 'r_0')}")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "google" and body["repeating"] is True and body["repeat_days"] == []
    assert body["start_date"] == _day(0) and body["start_time"] == "10:00:00"


# ── Editing ──────────────────────────────────────────────────────────────────

def test_editing_one_instance_changes_it_in_kanso_only(client, db, google):
    _series(client, db, google)
    response = client.put(
        f"/update-event/{_id(db, 'r_1')}",
        params={"scope": "this", "occurrence_date": _day(7)},
        json={"title": "Moved standup", "start_time": "14:00:00", "end_time": "15:30:00"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == _id(db, "r_1") and response.json()["source"] == "google"
    [event] = _on(client, 1)
    assert event["title"] == "Moved standup" and event["edited"] is True
    assert event["start_time"] == "14:00:00" and event["end_time"] == "15:30:00"
    assert _title(client, 0) == "Standup" and _title(client, 2) == "Standup"
    assert all(method == "GET" for method, url, _ in google.requests if "/calendars/" in url)


def test_one_instance_can_move_to_another_day(client, db, google):
    _series(client, db, google)
    client.put(f"/update-event/{_id(db, 'r_1')}", params={"scope": "this"}, json={"date": _day(8)})
    assert _on(client, 1) == []
    [moved] = _activity_events(client, _day(8))
    assert moved["start_time"] == "10:00:00" and moved["end_time"] == "11:00:00"


def test_a_one_off_event_moves_with_date_or_start_date(client, db, google):
    google.full[PRIMARY] = [_timed("one", updated=LONG_AGO)]
    _connect(client)
    event_id = _id(db, "one")
    assert client.put(f"/update-event/{event_id}", json={"start_date": _day(2)}).status_code == 200
    assert len(_activity_events(client, _day(2))) == 1
    assert client.put(f"/update-event/{event_id}", json={"date": _day(3), "all_day": True}).status_code == 200
    [event] = _activity_events(client, _day(3))
    assert event["all_day"] is True and event["start_time"] is None


def test_scope_this_refuses_series_fields_like_kanso_events(client, db, google):
    _series(client, db, google)
    response = client.put(f"/update-event/{_id(db, 'r_1')}", params={"scope": "this"}, json={"time_zone": TZ})
    assert response.status_code == 400


def test_following_and_all_cannot_move_the_day(client, db, google):
    _series(client, db, google)
    for scope in ("following", "all"):
        response = client.put(f"/update-event/{_id(db, 'r_1')}", params={"scope": scope}, json={"date": _day(8)})
        assert response.status_code == 400


def test_a_timed_edit_needs_both_times(client, db, google):
    google.full[PRIMARY] = [{"id": "day", "status": "confirmed", "summary": "Holiday",
                             "start": {"date": _day(0)}, "end": {"date": _day(1)}}]
    _connect(client)
    response = client.put(f"/update-event/{_id(db, 'day')}", json={"all_day": False, "start_time": "09:00:00"})
    assert response.status_code == 422


def test_following_edits_this_and_later_instances_and_ones_imported_later(client, db, google):
    _series(client, db, google)
    response = client.put(
        f"/update-event/{_id(db, 'r_1')}", params={"scope": "following"},
        json={"title": "Sync", "start_time": "09:00:00", "end_time": "09:15:00", "repeat_days": [1]},
    )
    assert response.status_code == 200
    assert [_title(client, n) for n in range(3)] == ["Standup", "Sync", "Sync"]
    assert _on(client, 2)[0]["start_time"] == "09:00:00"

    _import_later(db, google, _instance(3))
    assert _title(client, 3) == "Sync" and _on(client, 3)[0]["end_time"] == "09:15:00"


def test_all_edits_every_instance(client, db, google):
    _series(client, db, google)
    client.put(f"/update-event/{_id(db, 'r_1')}", params={"scope": "all"}, json={"description": "Room 4"})
    assert all(_on(client, n)[0]["description"] == "Room 4" for n in range(3))


def test_a_later_following_edit_takes_over_from_its_instance(client, db, google):
    _series(client, db, google, 4)
    client.put(f"/update-event/{_id(db, 'r_1')}", params={"scope": "following"}, json={"title": "A"})
    client.put(f"/update-event/{_id(db, 'r_2')}", params={"scope": "this"}, json={"title": "Own"})
    client.put(f"/update-event/{_id(db, 'r_3')}", params={"scope": "following"}, json={"title": "B"})
    _import_later(db, google, _instance(4))
    assert [_title(client, n) for n in range(5)] == ["Standup", "A", "Own", "B", "B"]


# ── Latest change wins ───────────────────────────────────────────────────────

def test_a_sync_keeps_a_kanso_edit_unless_google_changed_the_event_after_it(client, db, google):
    google.full[PRIMARY] = [_timed("a", updated=LONG_AGO)]
    _connect(client)
    client.put(f"/update-event/{_id(db, 'a')}", json={"title": "Kanso title"})

    # Google lists it again, unchanged since before the edit.
    google.expired_tokens.add(_row(db, "a").calendar.sync_token)
    _resync(db)
    assert _activity_events(client)[0]["title"] == "Kanso title"

    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    _import_later(db, google, _timed("a", summary="Google title", updated=later))
    [event] = _activity_events(client)
    assert event["title"] == "Google title" and event["edited"] is False


def test_a_series_edit_skips_new_instances_google_changed_after_it(client, db, google):
    _series(client, db, google)
    client.put(f"/update-event/{_id(db, 'r_0')}", params={"scope": "all"}, json={"title": "Kanso"})
    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    _import_later(db, google, {**_instance(3), "updated": later, "summary": "Google"})
    assert _title(client, 3) == "Google"


# ── Tags ─────────────────────────────────────────────────────────────────────

def test_tags_for_following_reach_later_instances_and_ones_imported_later(client, db, google):
    _series(client, db, google, 4)
    work, own = _tag(db, "Work"), _tag(db, "Own")
    client.patch(f"/google-calendar/events/{_id(db, 'r_3')}/tags", json={"tag_ids": [own.id]})

    response = client.patch(
        f"/google-calendar/events/{_id(db, 'r_1')}/tags", json={"tag_ids": [work.id], "scope": "following"}
    )
    assert [t["name"] for t in response.json()["tags"]] == ["Work"]
    assert _on(client, 0)[0]["tags"] == []
    assert [_tags(client, n) for n in (1, 2, 3)] == [["Work"], ["Work"], ["Work"]]

    _import_later(db, google, _instance(4))
    assert _tags(client, 4) == ["Work"]


def test_a_later_tag_change_overrides_one_instance_or_takes_over_from_it(client, db, google):
    _series(client, db, google, 4)
    work, own, late = _tag(db, "Work"), _tag(db, "Own"), _tag(db, "Late")
    url = "/google-calendar/events/{}/tags"
    client.patch(url.format(_id(db, "r_0")), json={"tag_ids": [work.id], "scope": "following"})
    client.patch(url.format(_id(db, "r_1")), json={"tag_ids": [own.id], "scope": "this"})
    client.patch(url.format(_id(db, "r_2")), json={"tag_ids": [late.id], "scope": "following"})
    _import_later(db, google, _instance(4))
    assert [_tags(client, n) for n in range(5)] == [["Work"], ["Own"], ["Late"], ["Late"], ["Late"]]


def test_tags_for_all_and_back_to_the_default(client, db, google):
    _series(client, db, google)
    work = _tag(db, "Work")
    url = f"/google-calendar/events/{_id(db, 'r_2')}/tags"
    client.patch(url, json={"tag_ids": [work.id], "scope": "all"})
    assert [_tags(client, n) for n in range(3)] == [["Work"]] * 3
    client.patch(url, json={"tag_ids": [], "use_default": True, "scope": "all"})
    assert [_tags(client, n) for n in range(3)] == [[]] * 3


def test_following_on_an_event_that_does_not_repeat_is_this(client, db, google):
    google.full[PRIMARY] = [_timed("a"), _timed("b")]
    _connect(client)
    work = _tag(db, "Work")
    client.patch(f"/google-calendar/events/{_id(db, 'a')}/tags", json={"tag_ids": [work.id], "scope": "following"})
    tags = {e["title"]: [t["name"] for t in e["tags"]] for e in _activity_events(client)}
    assert tags == {"Event a": ["Work"], "Event b": []}
    assert db.query(GoogleEventOverride).count() == 0


def test_tags_named_in_an_edit_apply_to_the_scope(client, db, google):
    _series(client, db, google)
    client.put(
        f"/update-event/{_id(db, 'r_1')}", params={"scope": "following"},
        json={"tags": [{"name": "Meetings", "color": "#abc"}]},
    )
    assert [_tags(client, n) for n in range(3)] == [[], ["Meetings"], ["Meetings"]]


# ── Archive ──────────────────────────────────────────────────────────────────

def test_archiving_one_instance_hides_it_if_upcoming_and_lists_it(client, db, google):
    _series(client, db, google)
    response = client.patch(f"/archive-event/{_id(db, 'r_1')}", params={"scope": "this"})
    assert response.status_code == 200 and response.json()["archived_at"]
    assert _on(client, 1) == []
    archived = client.get("/get-archived-event-occurrences", params={"tz": TZ}).json()
    assert [(e["event_id"], e["archived"], e["source"]) for e in archived] == [(_id(db, "r_1"), True, "google")]

    client.patch(f"/unarchive-event/{_id(db, 'r_1')}", params={"scope": "this"})
    assert _title(client, 1) == "Standup"


def test_an_archived_past_event_stays_on_the_calendar_flagged(client, db, google):
    google.full[PRIMARY] = [_timed("past", _at(-90, "10:00"), _at(-90, "11:00"))]
    _connect(client)
    client.patch(f"/archive-event/{_id(db, 'past')}")
    [event] = _activity_events(client, _day(-90))
    assert event["archived"] is True and event["past"] is True


def test_archiving_following_lists_one_series_and_reaches_ones_imported_later(client, db, google):
    _series(client, db, google)
    client.patch(f"/archive-event/{_id(db, 'r_1')}", params={"scope": "following"})
    assert _title(client, 0) == "Standup" and _on(client, 1) == [] and _on(client, 2) == []
    _import_later(db, google, _instance(3))
    assert _on(client, 3) == []

    archived = [e for e in client.get("/get-events", params={"archived": True}).json() if e["source"] == "google"]
    assert [(e["id"], e["repeating"]) for e in archived] == [(_id(db, "r_1"), True)]
    assert client.get("/get-archived-event-occurrences", params={"tz": TZ}).json() == []

    client.patch(f"/unarchive-event/{_id(db, 'r_1')}", params={"scope": "all"})
    assert [_title(client, n) for n in range(4)] == ["Standup"] * 4
    _import_later(db, google, _instance(4))
    assert _title(client, 4) == "Standup"


def test_an_archived_one_off_event_is_an_archived_series(client, db, google):
    google.full[PRIMARY] = [_timed("one")]
    _connect(client)
    client.patch(f"/archive-event/{_id(db, 'one')}")
    archived = client.get("/get-events", params={"archived": True}).json()
    assert [(e["id"], e["source"], e["repeating"]) for e in archived] == [(_id(db, "one"), "google", False)]
    assert client.get("/get-events").json() == []


# ── Trash ────────────────────────────────────────────────────────────────────

def _trash(client) -> list[dict]:
    return [item for item in client.get("/get-trash").json() if item["kind"] == "event"]


def test_deleting_one_instance_trashes_it_for_restore(client, db, google):
    _series(client, db, google)
    event_id = _id(db, "r_1")
    response = client.delete(f"/del-event/{event_id}", params={"scope": "this", "occurrence_date": _day(7)})
    assert response.status_code == 200 and response.json()["deleted_at"]
    assert _on(client, 1) == []
    [item] = _trash(client)
    assert item["id"] == event_id and item["occurrence_date"] == _day(7) and item["source"] == "google"
    assert client.put(f"/update-event/{event_id}", json={"title": "x"}).status_code == 404

    assert client.patch(f"/restore-event/{event_id}", params={"occurrence_date": _day(7)}).status_code == 200
    assert _title(client, 1) == "Standup" and _trash(client) == []


def test_deleting_following_is_one_trash_item_and_hides_ones_imported_later(client, db, google):
    _series(client, db, google)
    client.delete(f"/del-event/{_id(db, 'r_1')}", params={"scope": "following"})
    assert _title(client, 0) == "Standup" and _on(client, 1) == [] and _on(client, 2) == []
    [item] = _trash(client)
    assert item["id"] == _id(db, "r_1") and item["occurrence_date"] is None

    _import_later(db, google, _instance(3))
    assert _on(client, 3) == []

    assert client.patch(f"/restore-event/{_id(db, 'r_1')}").status_code == 200
    assert [_title(client, n) for n in range(4)] == ["Standup"] * 4
    _import_later(db, google, _instance(4))
    assert _title(client, 4) == "Standup"


def test_purging_deletes_for_good_and_a_sync_does_not_bring_it_back(client, db, google):
    _series(client, db, google)
    client.delete(f"/del-event/{_id(db, 'r_0')}", params={"scope": "all"})
    assert client.delete(f"/purge-event/{_id(db, 'r_0')}").status_code == 204
    assert _trash(client) == []
    assert client.patch(f"/restore-event/{_id(db, 'r_0')}").status_code == 404

    google.expired_tokens.add(_row(db, "r_0").calendar.sync_token)
    google.full[PRIMARY] = [_instance(n) for n in range(4)]
    _resync(db)
    assert all(_on(client, n) == [] for n in range(4))


def test_the_trash_expires_imported_events_like_kanso_ones(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    client.delete(f"/del-event/{_id(db, 'a')}")
    db.query(GoogleCalendarEvent).update({"deleted_at": datetime.now(UTC) - timedelta(days=31)})
    purge_expired(db)
    db.commit()
    assert _trash(client) == []
    assert _row(db, "a").cancelled is True


# ── Busy ─────────────────────────────────────────────────────────────────────

def test_declined_and_free_events_show_but_are_not_busy(client, db, google):
    google.full[PRIMARY] = [
        _timed("busy", _at(0, "09:00"), _at(0, "10:00")),
        _timed("free", _at(0, "10:00"), _at(0, "11:00"), transparency="transparent"),
        _timed("declined", _at(0, "11:00"), _at(0, "12:00"), attendees=[
            {"email": "other@example.com", "responseStatus": "accepted"},
            {"email": PRIMARY, "self": True, "responseStatus": "declined"},
        ]),
    ]
    _connect(client)
    db.add(Profile(user_id=USER_ID, name="Test", rest_days=[], day_start_time="08:00", shutoff_time="22:00"))
    db.commit()
    busy = {e["title"]: e["busy"] for e in _activity_events(client)}
    assert busy == {"Event busy": True, "Event free": False, "Event declined": False}

    report = client.get("/get-event-report", params={"start_date": _day(0), "end_date": _day(0), "tz": TZ}).json()
    # Declined and free events aren't counted.
    assert report["event_count"] == 1
    assert report["total_minutes"] == 60 and report["event_window_minutes"] == 60

    debrief = client.get(
        "/daily-debrief", params={"local_date": _day(0), "local_time": "08:00", "tz": TZ}
    ).json()
    assert debrief["workload"]["event_minutes"] == 60

    # A kanso edit doesn't make it busy.
    client.put(f"/update-event/{_id(db, 'free')}", json={"title": "Still free"})
    assert {e["title"]: e["busy"] for e in _activity_events(client)}["Still free"] is False


# ── Ownership and cleanup ────────────────────────────────────────────────────

def test_another_user_cannot_change_an_imported_event(client, db, google):
    _series(client, db, google)
    event_id = _id(db, "r_0")
    _as_other_user()
    assert client.put(f"/update-event/{event_id}", json={"title": "x"}).status_code == 404
    assert client.patch(f"/archive-event/{event_id}").status_code == 404
    assert client.delete(f"/del-event/{event_id}").status_code == 404
    assert client.get(f"/get-event/{event_id}").status_code == 404
    assert _row(db, "r_0").title == "Standup"


def test_disconnect_and_account_deletion_remove_overrides(client, db, google):
    _series(client, db, google)
    client.put(f"/update-event/{_id(db, 'r_0')}", params={"scope": "all"}, json={"title": "x"})
    assert db.query(GoogleEventOverride).count() == 1
    assert client.delete("/google-calendar/connection").status_code == 204
    assert db.query(GoogleEventOverride).count() == 0

    _connect(client)
    client.delete(f"/del-event/{_id(db, 'r_0')}", params={"scope": "following"})
    assert db.query(GoogleEventOverride).count() == 1
    account_crud.delete_user_rows(db, USER_ID)
    db.commit()
    assert db.query(GoogleEventOverride).count() == 0
    assert db.query(GoogleCalendarEvent).count() == 0
