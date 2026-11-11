"""The read-only Google Calendar import, with Google replaced by a fake."""

import base64
import logging
import os
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.core import crypto, google_calendar
from app.core.auth import UserInfo, get_current_user
from app.crud import account_crud
from app.crud.event_occurrence_crud import occurrences_between
from app.crud.google_calendar_crud import STALE_AFTER, get_connection, refresh_if_stale, sync_now
from app.main import app
from app.models.event_model import Event
from app.models.google_calendar_model import (
    GoogleCalendar,
    GoogleCalendarConnection,
    GoogleCalendarEvent,
    GoogleOAuthState,
)
from app.models.tag_model import Tag
from app.routers import google_calendar_router
from tests.conftest import OTHER_USER_ID, USER_ID

FAKE_REFRESH_TOKEN = "fake-refresh-token-do-not-log"
PRIMARY = "me@example.com"
FRONTEND = "http://localhost:3000"
TZ = "America/Los_Angeles"
# Inside the imported window, whenever the tests run.
START = date.today() + timedelta(days=60)


def _day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def _at(offset: int, clock: str) -> str:
    """A local time in TZ as Google writes it, with its UTC offset."""
    return datetime.fromisoformat(f"{_day(offset)}T{clock}:00").replace(tzinfo=ZoneInfo(TZ)).isoformat()


def _timed(event_id, start=_at(0, "10:00"), end=_at(0, "11:00"), **extra):
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": f"Event {event_id}",
        "start": {"dateTime": start, "timeZone": TZ},
        "end": {"dateTime": end, "timeZone": TZ},
        "htmlLink": f"https://www.google.com/calendar/event?eid={event_id}",
        **extra,
    }


class FakeGoogle:
    """Answers the token, revoke and Calendar endpoints the way Google does."""

    def __init__(self):
        self.calendars = [{
            "id": PRIMARY, "summary": "Mine", "backgroundColor": "#123456",
            "timeZone": TZ, "primary": True, "selected": True,
        }]
        self.full: dict[str, list[dict]] = {PRIMARY: []}
        self.changes: dict[str, list[dict]] = {}
        self.expired_tokens: set[str] = set()
        self.token_response = httpx.Response(200, json={
            "access_token": "access",
            "refresh_token": FAKE_REFRESH_TOKEN,
            "scope": " ".join(google_calendar.SCOPES),
        })
        self.refresh_response = httpx.Response(200, json={"access_token": "access", "scope": ""})
        self.api_status: int | None = None
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.sync_counter = 0

    def __call__(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url == google_calendar.TOKEN_URL:
            is_code = kwargs["data"]["grant_type"] == "authorization_code"
            return self.token_response if is_code else self.refresh_response
        if url == google_calendar.REVOKE_URL:
            self.revoked.append(kwargs["data"]["token"])
            return httpx.Response(200)
        assert method == "GET", "the import must never write to Google"
        if self.api_status is not None:
            return httpx.Response(self.api_status, json={})
        if url.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": self.calendars})
        calendar_id = url.split("/calendars/")[1].split("/events")[0].replace("%40", "@")
        token = kwargs["params"].get("syncToken")
        if token in self.expired_tokens:
            return httpx.Response(410, json={})
        self.sync_counter += 1
        items = self.changes.get(calendar_id, []) if token else self.full.get(calendar_id, [])
        return httpx.Response(200, json={"items": items, "nextSyncToken": f"token-{self.sync_counter}"})

    def event_calls(self):
        return [params for _, url, kw in self.requests if url.endswith("/events") for params in [kw["params"]]]


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    google_calendar_router._google_call_limiter._calls.clear()


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv(crypto.KEY_ENV, base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def configured(monkeypatch, key_env):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://api.example.com/google-calendar/callback")


@pytest.fixture
def google(monkeypatch, configured):
    fake = FakeGoogle()
    monkeypatch.setattr(google_calendar, "_send", fake)
    return fake


def _state(authorization_url: str) -> str:
    return parse_qs(urlsplit(authorization_url).query)["state"][0]


def _start(client) -> str:
    response = client.post("/google-calendar/connect", headers={"Origin": FRONTEND})
    assert response.status_code == 200
    return _state(response.json()["authorization_url"])


def _callback(client, state):
    return client.get("/google-calendar/callback", params={"code": "c", "state": state}, follow_redirects=False)


def _complete(client, state):
    return client.post("/google-calendar/complete", json={"code": "c", "state": state})


def _exchanges(google) -> list:
    return [r for r in google.requests if r[1] == google_calendar.TOKEN_URL]


def _connect(client) -> None:
    state = _start(client)
    response = _callback(client, state)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{FRONTEND}/settings"
    assert parse_qs(location.query) == {"google_calendar": ["complete"], "code": ["c"], "state": [state]}
    assert _complete(client, state).status_code == 200


def _resync(db) -> None:
    """Let the next sync run, as if the last was long ago."""
    db.query(GoogleCalendarConnection).update({"last_attempt_at": None})
    db.commit()
    sync_now(db, USER_ID)


def _activity_events(client, day=_day(0)):
    response = client.get(
        "/get-calendar-activity", params={"start_date": day, "end_date": day, "tz": TZ}
    )
    assert response.status_code == 200
    return response.json()["events"]


def _tag(db, name="Work", user_id=USER_ID) -> Tag:
    tag = Tag(name=name, color="#fff", user_id=user_id)
    db.add(tag)
    db.commit()
    return tag


def _row(db, google_event_id) -> GoogleCalendarEvent | None:
    db.expire_all()
    return db.query(GoogleCalendarEvent).filter(GoogleCalendarEvent.google_event_id == google_event_id).first()


def _calendar(db) -> GoogleCalendar:
    db.expire_all()
    return db.query(GoogleCalendar).filter(GoogleCalendar.google_calendar_id == PRIMARY).one()


# ── Unconfigured ─────────────────────────────────────────────────────────────

def test_unconfigured_reports_it_and_refuses_cleanly(client):
    assert client.get("/google-calendar/status").json() == {
        "configured": False, "connected": False, "email": None, "last_synced_at": None, "last_error": None,
    }
    for response in (
        client.post("/google-calendar/connect"),
        client.post("/google-calendar/complete", json={"code": "c", "state": "s"}),
        client.post("/google-calendar/sync"),
    ):
        assert response.status_code == 503
        assert response.json()["detail"] == "google_calendar_not_configured"
    assert client.get("/google-calendar/calendars").json() == []
    assert _activity_events(client) == []


def test_oauth_values_without_an_encryption_key_are_not_configured(client, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV, raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "a")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "b")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "c")
    assert client.get("/google-calendar/status").json()["configured"] is False


def test_demo_sandboxes_cannot_connect(client, google):
    app.dependency_overrides[get_current_user] = lambda: UserInfo(USER_ID, "", "anonymous", True)
    assert client.get("/google-calendar/status").json()["configured"] is False
    assert client.post("/google-calendar/connect").status_code == 403
    assert client.post("/google-calendar/complete", json={"code": "c", "state": "s"}).status_code == 403


# ── OAuth ────────────────────────────────────────────────────────────────────

def test_connect_asks_only_for_read_only_offline_access(client, google):
    response = client.post("/google-calendar/connect", headers={"Origin": FRONTEND})
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert set(query["scope"][0].split()) == {
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    }
    assert query["access_type"] == ["offline"]
    assert query["response_type"] == ["code"]


def test_the_callback_alone_connects_nothing(client, db, google):
    state = _start(client)
    assert _callback(client, state).status_code == 303
    assert not _exchanges(google)
    assert db.query(GoogleCalendarConnection).count() == 0
    # The state is still unspent for the signed-in step.
    assert _complete(client, state).status_code == 200
    assert get_connection(db, USER_ID) is not None


def test_a_different_signed_in_user_cannot_complete(client, db, google):
    state = _start(client)
    _callback(client, state)
    _as_other_user()
    response = _complete(client, state)
    assert response.status_code == 403 and response.json()["detail"] == "google_invalid_state"
    assert not _exchanges(google)
    assert db.query(GoogleCalendarConnection).count() == 0


def test_complete_rejects_an_unknown_state(client, db, google):
    assert _complete(client, "forged").status_code == 403
    assert not _exchanges(google)
    assert db.query(GoogleCalendarConnection).count() == 0


@pytest.mark.parametrize("state", ["forged", ""])
def test_callback_rejects_an_unknown_state(client, db, google, state):
    response = client.get("/google-calendar/callback", params={"code": "c", "state": state}, follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query == {"google_calendar": ["error"], "reason": ["google_invalid_state"]}
    assert db.query(GoogleCalendarConnection).count() == 0
    assert not _exchanges(google)


def test_a_state_completes_once(client, db, google):
    state = _start(client)
    assert _complete(client, state).status_code == 200
    calls = len(_exchanges(google))
    assert _complete(client, state).status_code == 403
    assert len(_exchanges(google)) == calls
    assert "google_invalid_state" in _callback(client, state).headers["location"]


def test_an_expired_state_is_refused(client, db, google):
    state = _start(client)
    db.query(GoogleOAuthState).update({"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    db.commit()
    assert "google_invalid_state" in _callback(client, state).headers["location"]
    assert _complete(client, state).status_code == 403
    assert not _exchanges(google)
    assert db.query(GoogleCalendarConnection).count() == 0


def test_callback_reports_a_refused_consent(client, db, google):
    response = client.get(
        "/google-calendar/callback", params={"error": "access_denied", "state": _start(client)}, follow_redirects=False
    )
    assert response.headers["location"].startswith(f"{FRONTEND}/settings?google_calendar=error")
    assert "google_access_denied" in response.headers["location"]
    assert db.query(GoogleCalendarConnection).count() == 0


def test_complete_refuses_a_partial_grant_and_revokes_it(client, db, google):
    google.token_response = httpx.Response(200, json={
        "access_token": "a", "refresh_token": FAKE_REFRESH_TOKEN, "scope": google_calendar.SCOPES[0],
    })
    response = _complete(client, _start(client))
    assert response.status_code == 422 and response.json()["detail"] == "google_scopes_missing"
    assert google.revoked == [FAKE_REFRESH_TOKEN]
    assert db.query(GoogleCalendarConnection).count() == 0


def test_an_unlisted_origin_never_becomes_the_redirect(client, db, google):
    url = client.post("/google-calendar/connect", headers={"Origin": "https://evil.example"}).json()
    response = client.get(
        "/google-calendar/callback",
        params={"code": "c", "state": _state(url["authorization_url"])},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("https://kanso-web-app.vercel.app/settings?")


# ── Token encryption ─────────────────────────────────────────────────────────

def test_refresh_token_is_stored_encrypted_and_never_returned_or_logged(client, db, google, caplog):
    with caplog.at_level(logging.DEBUG):
        _connect(client)
        status = client.get("/google-calendar/status")
    row = get_connection(db, USER_ID)
    assert FAKE_REFRESH_TOKEN.encode() not in bytes(row.token_ciphertext)
    decrypted = crypto.decrypt(
        crypto.Encrypted(bytes(row.token_ciphertext), bytes(row.token_nonce), row.key_version),
        f"google-calendar:{USER_ID}",
    )
    assert decrypted == FAKE_REFRESH_TOKEN
    assert FAKE_REFRESH_TOKEN not in status.text
    assert FAKE_REFRESH_TOKEN not in caplog.text


def test_a_token_copied_to_another_user_does_not_decrypt(client, db, google):
    _connect(client)
    row = get_connection(db, USER_ID)
    db.add(GoogleCalendarConnection(
        user_id=OTHER_USER_ID,
        token_ciphertext=row.token_ciphertext,
        token_nonce=row.token_nonce,
        key_version=row.key_version,
    ))
    db.commit()
    sync_now(db, OTHER_USER_ID)
    assert get_connection(db, OTHER_USER_ID).last_error == "google_token_unreadable"


# ── Sync ─────────────────────────────────────────────────────────────────────

def test_first_sync_imports_events_with_the_occurrence_shape(client, db, google):
    google.full[PRIMARY] = [_timed("a", description="Notes")]
    _connect(client)

    status = client.get("/google-calendar/status").json()
    assert status["connected"] is True and status["email"] == PRIMARY and status["last_error"] is None
    assert status["last_synced_at"]

    [event] = _activity_events(client)
    assert event["source"] == "google" and event["read_only"] is False
    assert event["repeating"] is False and event["busy"] is True
    assert event["event_id"] < 0
    assert event["title"] == "Event a"
    assert event["description"] == "Notes"
    assert event["date"] == _day(0)
    assert event["start_time"] == "10:00:00" and event["end_time"] == "11:00:00"
    assert event["html_link"] == "https://www.google.com/calendar/event?eid=a"


def test_full_sync_expands_repeating_events_and_is_bounded(client, db, google):
    google.full[PRIMARY] = [_timed("r_1", recurringEventId="r")]
    _connect(client)
    [params] = google.event_calls()
    assert params["singleEvents"] == "true"
    assert params["timeMin"] and params["timeMax"]
    assert _row(db, "r_1").recurring_event_id == "r"


def test_incremental_sync_uses_the_token_and_applies_only_changes(client, db, google):
    google.full[PRIMARY] = [_timed("a"), _timed("b")]
    _connect(client)
    token = _calendar(db).sync_token
    assert token

    google.changes[PRIMARY] = [_timed("a", summary="Renamed"), _timed("c")]
    _resync(db)

    params = google.event_calls()[-1]
    assert params["syncToken"] == token
    assert "timeMin" not in params and "timeMax" not in params
    assert _row(db, "a").title == "Renamed"
    assert _row(db, "b") is not None
    assert _row(db, "c") is not None
    assert _calendar(db).sync_token != token


def test_an_expired_token_falls_back_to_a_full_sync_keeping_tags(client, db, google):
    google.full[PRIMARY] = [_timed("a"), _timed("gone")]
    _connect(client)
    tag = _tag(db)
    event_id = -_row(db, "a").id
    assert client.patch(f"/google-calendar/events/{event_id}/tags", json={"tag_ids": [tag.id]}).status_code == 200

    google.expired_tokens.add(_calendar(db).sync_token)
    google.full[PRIMARY] = [_timed("a", summary="Still here")]
    _resync(db)

    row = _row(db, "a")
    assert row.title == "Still here"
    assert [t.id for t in row.tags] == [tag.id] and row.tags_overridden is True
    assert _row(db, "gone") is None
    assert get_connection(db, USER_ID).last_error is None


def test_cancelled_events_are_removed(client, db, google):
    google.full[PRIMARY] = [_timed("a"), _timed("r_1", recurringEventId="r"), _timed("r_2", recurringEventId="r")]
    _connect(client)

    google.changes[PRIMARY] = [{"id": "a", "status": "cancelled"}, {"id": "r", "status": "cancelled"}]
    _resync(db)

    assert _row(db, "a") is None
    assert _row(db, "r_1") is None and _row(db, "r_2") is None
    assert _activity_events(client) == []


def test_a_full_sync_keeps_history_older_than_its_window(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    old = _row(db, "a")
    old.starts_at = datetime.now(UTC) - timedelta(days=800)
    old.ends_at = old.starts_at + timedelta(hours=1)
    db.commit()

    google.full[PRIMARY] = []
    google.expired_tokens.add(_calendar(db).sync_token)
    _resync(db)
    assert _row(db, "a") is not None


def test_the_window_moves_forward_with_a_full_sync(client, db, google):
    _connect(client)
    calendar = _calendar(db)
    calendar.synced_through = datetime.now(UTC) + timedelta(days=300)
    db.commit()
    _resync(db)
    assert "timeMin" in google.event_calls()[-1]
    assert _calendar(db).synced_through > datetime.now(UTC) + timedelta(days=390)


def test_deselected_calendars_are_not_synced_or_shown_but_keep_their_tags(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    tag = _tag(db)
    client.patch(f"/google-calendar/events/{-_row(db, 'a').id}/tags", json={"tag_ids": [tag.id]})
    calendar_id = client.get("/google-calendar/calendars").json()[0]["id"]

    deselected = client.patch(f"/google-calendar/calendars/{calendar_id}", json={"selected": False})
    assert deselected.json()["selected"] is False
    assert _activity_events(client) == []
    calls = len(google.event_calls())
    _resync(db)
    assert len(google.event_calls()) == calls

    client.patch(f"/google-calendar/calendars/{calendar_id}", json={"selected": True})
    [event] = _activity_events(client)
    assert [t["id"] for t in event["tags"]] == [tag.id]


def test_a_calendar_removed_from_google_goes_with_its_events(client, db, google):
    other = {"id": "team@example.com", "summary": "Team", "timeZone": "UTC", "selected": True}
    google.calendars.append(other)
    google.full[other["id"]] = [_timed("t")]
    _connect(client)
    assert _row(db, "t") is not None
    google.calendars.remove(other)
    _resync(db)
    assert _row(db, "t") is None
    assert [c["name"] for c in client.get("/google-calendar/calendars").json()] == ["Mine"]


def test_new_calendars_start_selected_only_if_shown_in_google(client, db, google):
    google.calendars.append({"id": "hidden@example.com", "summary": "Hidden", "timeZone": "UTC"})
    _connect(client)
    assert {c["name"]: c["selected"] for c in client.get("/google-calendar/calendars").json()} == {
        "Mine": True, "Hidden": False,
    }


def test_one_inaccessible_calendar_does_not_stop_the_others(client, db, google, monkeypatch):
    google.calendars.append({"id": "lost@example.com", "summary": "Lost", "timeZone": "UTC", "selected": True})
    google.full[PRIMARY] = [_timed("a")]
    real = google.__call__

    def lost_calendar(method, url, **kwargs):
        if "lost%40example.com" in url:
            return httpx.Response(404, json={})
        return real(method, url, **kwargs)

    monkeypatch.setattr(google_calendar, "_send", lost_calendar)
    _connect(client)
    assert _row(db, "a") is not None
    assert get_connection(db, USER_ID).last_error is None


def test_parse_skips_working_location_entries():
    item = _timed("w", eventType="workingLocation")
    assert google_calendar.parse_event(item, "UTC") is None


def test_parse_only_keeps_https_links():
    event = google_calendar.parse_event(_timed("a", htmlLink="javascript:alert(1)"), "UTC")
    assert event.html_link is None


# ── Staleness and failure ────────────────────────────────────────────────────

def test_reads_refresh_only_when_stale(client, db, google):
    _connect(client)
    calls = len(google.requests)
    _activity_events(client)
    assert len(google.requests) == calls

    db.query(GoogleCalendarConnection).update(
        {"last_attempt_at": datetime.now(UTC) - STALE_AFTER - timedelta(seconds=1)}
    )
    db.commit()
    _activity_events(client)
    assert len(google.requests) > calls


def test_a_revoked_connection_reports_it_and_keeps_serving_stored_events(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    google.refresh_response = httpx.Response(400, json={"error": "invalid_grant"})
    db.query(GoogleCalendarConnection).update({"last_attempt_at": None})
    db.commit()

    assert len(_activity_events(client)) == 1
    status = client.get("/google-calendar/status").json()
    assert status["connected"] is True and status["last_error"] == "google_access_revoked"
    report = client.get(
        "/get-event-report", params={"start_date": _day(0), "end_date": _day(0), "tz": TZ}
    )
    assert report.status_code == 200


def test_a_google_outage_is_reported_by_sync_and_cleared_by_the_next(client, db, google):
    _connect(client)
    google.api_status = 500
    db.query(GoogleCalendarConnection).update({"last_attempt_at": None})
    db.commit()
    response = client.post("/google-calendar/sync")
    assert response.status_code == 200 and response.json()["last_error"] == "google_unavailable"

    google.api_status = None
    db.query(GoogleCalendarConnection).update({"last_attempt_at": None})
    db.commit()
    assert client.post("/google-calendar/sync").json()["last_error"] is None


def test_network_errors_never_reach_the_calendar_endpoints(client, db, google, monkeypatch):
    _connect(client)

    def down(*_, **__):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(google_calendar, "_send", down)
    db.query(GoogleCalendarConnection).update({"last_attempt_at": None})
    db.commit()
    assert _activity_events(client) == []
    assert get_connection(db, USER_ID).last_error == "google_unavailable"


def test_refresh_if_stale_swallows_unexpected_errors(db, google, monkeypatch):
    def broken(*_):
        raise RuntimeError("secret-ish detail")

    monkeypatch.setattr("app.crud.google_calendar_crud._claim", broken)
    refresh_if_stale(db, USER_ID)


def test_sync_without_a_connection_is_404(client, google):
    response = client.post("/google-calendar/sync")
    assert response.status_code == 404 and response.json()["detail"] == "google_calendar_not_connected"


def test_no_transaction_stays_open_while_waiting_on_google(client, db, google, monkeypatch):
    google.full[PRIMARY] = [_timed("a")]
    open_during: list[str] = []
    calls: list[str] = []

    def watching(method, url, **kwargs):
        calls.append(url)
        if db.in_transaction():
            open_during.append(url)
        return google(method, url, **kwargs)

    monkeypatch.setattr(google_calendar, "_send", watching)
    _connect(client)
    _resync(db)
    assert client.delete("/google-calendar/connection").status_code == 204

    assert any(url.endswith("/events") for url in calls)
    assert google_calendar.REVOKE_URL in calls
    assert open_during == []


def test_a_rotated_refresh_token_is_kept_even_if_the_sync_then_fails(client, db, google):
    _connect(client)
    google.refresh_response = httpx.Response(200, json={
        "access_token": "access", "refresh_token": "rotated-token", "scope": "",
    })
    google.api_status = 500
    _resync(db)
    db.expire_all()
    row = get_connection(db, USER_ID)
    assert row.last_error == "google_unavailable"
    assert crypto.decrypt(
        crypto.Encrypted(bytes(row.token_ciphertext), bytes(row.token_nonce), row.key_version),
        f"google-calendar:{USER_ID}",
    ) == "rotated-token"


def test_a_second_sync_right_after_the_first_does_nothing(client, db, google):
    _connect(client)
    calls = len(google.requests)
    assert client.post("/google-calendar/sync").status_code == 200
    assert len(google.requests) == calls


# ── Disconnect ───────────────────────────────────────────────────────────────

def test_disconnect_revokes_and_deletes_everything_imported(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    tag = _tag(db)
    client.patch(f"/google-calendar/events/{-_row(db, 'a').id}/tags", json={"tag_ids": [tag.id]})

    assert client.delete("/google-calendar/connection").status_code == 204

    assert google.revoked == [FAKE_REFRESH_TOKEN]
    for model in (GoogleCalendarConnection, GoogleCalendar, GoogleCalendarEvent):
        assert db.query(model).count() == 0
    assert db.get(Tag, tag.id) is not None
    assert client.get("/google-calendar/status").json()["connected"] is False


def test_disconnect_still_deletes_when_google_cannot_be_reached(client, db, google, monkeypatch):
    _connect(client)

    def down(*_, **__):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(google_calendar, "_send", down)
    assert client.delete("/google-calendar/connection").status_code == 204
    assert db.query(GoogleCalendarConnection).count() == 0


def test_account_deletion_removes_the_import(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    account_crud.delete_user_rows(db, USER_ID)
    db.commit()
    assert db.query(GoogleCalendarConnection).count() == 0
    assert db.query(GoogleCalendarEvent).count() == 0


# ── Tags ─────────────────────────────────────────────────────────────────────

def _tags_of(client) -> list[str]:
    return [t["name"] for t in _activity_events(client)[0]["tags"]]


def test_tag_precedence_own_then_calendar_default_then_none(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    default, own = _tag(db, "Default"), _tag(db, "Own")
    event_id = -_row(db, "a").id
    calendar_id = client.get("/google-calendar/calendars").json()[0]["id"]
    tags_url = f"/google-calendar/events/{event_id}/tags"
    assert _tags_of(client) == []

    response = client.patch(f"/google-calendar/calendars/{calendar_id}", json={"default_tag_id": default.id})
    assert response.json()["default_tag"]["name"] == "Default"
    assert _tags_of(client) == ["Default"]

    response = client.patch(tags_url, json={"tag_ids": [own.id]})
    assert response.json()["event_id"] == event_id and [t["name"] for t in response.json()["tags"]] == ["Own"]
    assert _tags_of(client) == ["Own"]

    assert client.patch(tags_url, json={"tag_ids": []}).json()["tags"] == []
    assert _tags_of(client) == []

    assert [t["name"] for t in client.patch(tags_url, json={"tag_ids": [], "use_default": True}).json()["tags"]] == [
        "Default"
    ]
    assert _tags_of(client) == ["Default"]

    assert client.patch(f"/google-calendar/calendars/{calendar_id}", json={"default_tag_id": None}).json()[
        "default_tag"
    ] is None
    assert _tags_of(client) == []


def test_use_default_with_tags_is_rejected(client, db, google):
    assert client.patch(
        "/google-calendar/events/-1/tags", json={"tag_ids": [1], "use_default": True}
    ).status_code == 422


def test_leaving_selected_out_keeps_it(client, db, google):
    _connect(client)
    calendar_id = client.get("/google-calendar/calendars").json()[0]["id"]
    assert client.patch(f"/google-calendar/calendars/{calendar_id}", json={}).json()["selected"] is True


def test_a_trashed_default_tag_is_hidden(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    tag = _tag(db, "Trashed")
    calendar_id = client.get("/google-calendar/calendars").json()[0]["id"]
    client.patch(f"/google-calendar/calendars/{calendar_id}", json={"default_tag_id": tag.id})
    assert client.delete(f"/del-tag/{tag.id}").status_code == 200
    db.expire_all()
    assert client.get("/google-calendar/calendars").json()[0]["default_tag"] is None
    assert _tags_of(client) == []


# ── Read-only ────────────────────────────────────────────────────────────────

def test_the_import_only_reads_from_google(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    tag = _tag(db)
    client.patch(f"/google-calendar/events/{-_row(db, 'a').id}/tags", json={"tag_ids": [tag.id]})
    api_calls = [r for r in google.requests if r[1].startswith(google_calendar.API_URL)]
    assert api_calls and all(method == "GET" for method, _, _ in api_calls)


def test_kanso_occurrences_say_where_they_came_from(client, db, google):
    created = client.post("/create-event", json={
        "title": "Mine", "start_date": _day(0), "start_time": "09:00:00", "end_time": "10:00:00", "time_zone": TZ,
    })
    assert created.json()["source"] == "kanso" and "read_only" not in created.json()
    [event] = _activity_events(client)
    assert event["source"] == "kanso" and event["read_only"] is False and event["html_link"] is None
    assert event["busy"] is True and event["repeating"] is False


# ── Counting ─────────────────────────────────────────────────────────────────

def test_imported_events_count_in_the_report_like_kanso_events(client, db, google):
    client.post("/create-event", json={
        "title": "Kanso", "start_date": _day(0), "start_time": "09:00:00", "end_time": "10:00:00", "time_zone": TZ,
    })
    google.full[PRIMARY] = [
        _timed("overlap", _at(0, "09:30"), _at(0, "10:30")),
        {"id": "day", "status": "confirmed", "summary": "Holiday",
         "start": {"date": _day(0)}, "end": {"date": _day(1)}},
    ]
    _connect(client)
    report = client.get(
        "/get-event-report", params={"start_date": _day(0), "end_date": _day(0), "tz": TZ}
    ).json()
    assert report["event_count"] == 3
    # 09:00-10:30, overlaps once, and the all-day event adds none.
    assert report["total_minutes"] == 90
    # 09:00-10:30 inside the window: overlaps count once, as for kanso events.
    assert report["event_window_minutes"] == 90


def test_the_daily_debrief_counts_imported_events(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    debrief = client.get(
        "/daily-debrief", params={"local_date": _day(0), "local_time": "08:00", "tz": TZ}
    ).json()
    assert [e["source"] for e in debrief["events_today"]] == ["google"]


def test_a_multi_day_all_day_event_keeps_each_day_in_every_time_zone(client, db, google):
    google.full[PRIMARY] = [{
        "id": "trip", "status": "confirmed", "summary": "Trip",
        "start": {"date": _day(0)}, "end": {"date": _day(2)},
    }]
    _connect(client)
    for tz in (TZ, "Asia/Tokyo"):
        occurrences = occurrences_between(db, USER_ID, date.fromisoformat(_day(-1)), date.fromisoformat(_day(3)), None)
        assert [occ.day for occ in occurrences] == [date.fromisoformat(_day(0)), date.fromisoformat(_day(1))]
        events = client.get(
            "/get-calendar-activity", params={"start_date": _day(-1), "end_date": _day(3), "tz": tz}
        ).json()["events"]
        assert [(e["date"], e["all_day"], e["start_time"]) for e in events] == [
            (_day(0), True, None), (_day(1), True, None),
        ]


def test_an_event_past_midnight_shows_on_both_days(client, db, google):
    google.full[PRIMARY] = [_timed("late", _at(0, "23:00"), _at(1, "01:00"))]
    _connect(client)
    assert len(_activity_events(client, _day(0))) == 1
    assert len(_activity_events(client, _day(1))) == 1


# ── User isolation ───────────────────────────────────────────────────────────

def _as_other_user():
    app.dependency_overrides[get_current_user] = lambda: UserInfo(OTHER_USER_ID, "o@example.com", "email", False)


def test_another_user_sees_and_changes_nothing(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    event_id = -_row(db, "a").id
    calendar_id = _calendar(db).id
    other_tag = _tag(db, "Theirs", OTHER_USER_ID)

    _as_other_user()
    assert _activity_events(client) == []
    assert client.get("/google-calendar/status").json()["connected"] is False
    assert client.get("/google-calendar/calendars").json() == []
    assert client.patch(f"/google-calendar/calendars/{calendar_id}", json={"selected": False}).status_code == 404
    assert client.patch(f"/google-calendar/events/{event_id}/tags", json={"tag_ids": [other_tag.id]}).status_code == 404
    assert client.delete("/google-calendar/connection").status_code == 204

    assert get_connection(db, USER_ID) is not None
    assert _calendar(db).selected is True
    assert _row(db, "a") is not None


def test_another_users_tag_cannot_be_used(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    theirs = _tag(db, "Theirs", OTHER_USER_ID)
    calendar_id = client.get("/google-calendar/calendars").json()[0]["id"]
    event_id = -_row(db, "a").id

    default = client.patch(f"/google-calendar/calendars/{calendar_id}", json={"default_tag_id": theirs.id})
    assert default.status_code == 404
    assert client.patch(f"/google-calendar/events/{event_id}/tags", json={"tag_ids": [theirs.id]}).status_code == 404
    assert db.execute(select(GoogleCalendar.default_tag_id)).scalar_one() is None


def test_a_kanso_event_id_is_not_an_imported_event(client, db, google):
    google.full[PRIMARY] = [_timed("a")]
    _connect(client)
    row_id = _row(db, "a").id
    assert client.patch(f"/google-calendar/events/{row_id}/tags", json={"tag_ids": []}).status_code == 404
    assert db.query(Event).count() == 0
