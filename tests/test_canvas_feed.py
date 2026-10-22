"""Canvas calendar feed connections: the link check, the fetch, parsing, the panel, and tasks added from feed items."""
# The Canvas fixtures are shared with test_canvas_read.py by importing them.
# ruff: noqa: F811

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core import canvas_cache, canvas_client, crypto
from app.core.canvas_client import CanvasNotCanvas, CanvasUrlInvalid
from app.core.canvas_feed import CanvasFeed, CanvasFeedInvalid, CanvasFeedUrlInvalid, normalize_feed_url, parse
from app.crud.canvas_crud import decrypt_token, get_connection
from app.models.canvas_connection_model import CanvasConnection
from app.models.task_model import Task
from tests.conftest import USER_ID
from tests.test_canvas_read import (  # noqa: F401 (fixtures)
    BASE_URL,
    HOST,
    PUBLIC_IP,
    TOKEN,
    canvas,
    connected,
    demo_client,
    fresh_state,
    sleeps,
)

CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789wxyz"
FEED_URL = f"{BASE_URL}/feeds/calendars/user_{CODE}.ics"
NEW_CODE = "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210abcd"
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(UTC)


def _ics_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _event(uid, summary, start, url=None, all_day=False) -> str:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}"]
    lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}" if all_day else f"DTSTART:{_ics_time(start)}")
    if url:
        lines.append(f"URL;VALUE=URI:{url}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _calendar(*events: str) -> bytes:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Instructure//Canvas//EN", "X-WR-CALNAME:Ada Lovelace Calendar",
        *events, "END:VCALENDAR", "",
    ]).encode()


SOON = NOW + timedelta(days=2)
LATER = NOW + timedelta(days=4)
FAR = NOW + timedelta(days=30)
PAST = NOW - timedelta(days=3)
ALL_DAY = (NOW.astimezone(LA) + timedelta(days=3)).date()


def _calendar_url(course_id, anchor):
    return f"{BASE_URL}/calendar?include_contexts=course_{course_id}&month=10&year=2026#{anchor}"


def _feed_events() -> list[str]:
    return [
        _event("event-assignment-101", "Problem Set 6 [CS 460-01 Fall 2026]", SOON,
               _calendar_url(11, "assignment_101")),
        _event("event-assignment-102", "Speech outline [COMM 103]", ALL_DAY, _calendar_url(12, "assignment_102"),
               all_day=True),
        _event("event-calendar-event-55", "Office hours [CS 460-01 Fall 2026]", LATER,
               f"{BASE_URL}/calendar?include_contexts=course_11&event_id=55"),
        _event("event-calendar-event-56", "Dentist", SOON, "https://evil.example.com/phish"),
        _event("event-assignment-103", "Way later [CS 460-01 Fall 2026]", FAR, _calendar_url(11, "assignment_103")),
        _event("event-assignment-104", "Already due [CS 460-01 Fall 2026]", PAST, _calendar_url(11, "assignment_104")),
    ]


class FeedHost:
    """The school's Canvas serving one user's calendar feed. Anything else falls through to the API fake."""

    def __init__(self):
        self.codes = {CODE, NEW_CODE}
        self.events = _feed_events()
        self.requests: list[httpx.Request] = []
        # Set to a callable to answer feed requests differently.
        self.respond = None

    def __call__(self, request: httpx.Request) -> httpx.Response | None:
        if not request.url.path.startswith("/feeds/"):
            return None
        self.requests.append(request)
        if self.respond:
            return self.respond(request)
        code = request.url.path.removeprefix("/feeds/calendars/user_").removesuffix(".ics")
        if code not in self.codes:
            return httpx.Response(404, text="The page you were looking for doesn't exist")
        return httpx.Response(200, content=_calendar(*self.events), headers={"Content-Type": "text/calendar"})


@pytest.fixture
def feed(canvas):
    host = FeedHost()
    canvas.respond = host
    return host


@pytest.fixture
def feed_connected(client, feed):
    """client, connected with the calendar feed, with the connect fetch forgotten."""
    response = client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    assert response.status_code == 200, response.text
    feed.requests.clear()
    return client


def _overview(client):
    response = client.get("/canvas/overview", params={"tz": "America/Los_Angeles"})
    assert response.status_code == 200, response.text
    return response.json()


# ── The feed link ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        FEED_URL, f"  {FEED_URL}  ", f"https://SDSU.Instructure.com/feeds/calendars/user_{CODE}.ics", f"{FEED_URL}#x",
        f"webcal://{HOST}/feeds/calendars/user_{CODE}.ics", f"WebCal://{HOST}/feeds/calendars/user_{CODE}.ics",
    ],
)
def test_feed_links_are_normalized(raw):
    feed = normalize_feed_url(raw)
    assert feed.url == FEED_URL
    assert feed.base_url == BASE_URL
    assert feed.last4 == "wxyz"


@pytest.mark.parametrize(
    "raw",
    [
        f"http://{HOST}/feeds/calendars/user_{CODE}.ics",
        f"webcals://{HOST}/feeds/calendars/user_{CODE}.ics",
        f"ftp://{HOST}/feeds/calendars/user_{CODE}.ics",
        f"webcal://10.0.0.5/feeds/calendars/user_{CODE}.ics",
        f"{HOST}/feeds/calendars/user_{CODE}.ics",
        f"https://10.0.0.5/feeds/calendars/user_{CODE}.ics",
        f"https://[::1]/feeds/calendars/user_{CODE}.ics",
        f"https://{HOST}:8443/feeds/calendars/user_{CODE}.ics",
        f"https://ada:pw@{HOST}/feeds/calendars/user_{CODE}.ics",
        f"https://localhost/feeds/calendars/user_{CODE}.ics",
    ],
)
def test_unsafe_feed_links_are_refused(raw):
    with pytest.raises(CanvasUrlInvalid):
        normalize_feed_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        f"{BASE_URL}/calendar",
        f"{BASE_URL}/feeds/calendars/course_{CODE}.ics",
        f"{BASE_URL}/feeds/calendars/user_short.ics",
        f"{BASE_URL}/feeds/calendars/user_{CODE}.ics?evil=1",
        f"{BASE_URL}/feeds/calendars/user_{CODE}.ics/../../api/v1/users/self",
        f"{BASE_URL}/api/v1/feeds/calendars/user_{CODE}.ics",
    ],
)
def test_https_links_that_are_not_canvas_feeds_are_refused(raw):
    with pytest.raises(CanvasFeedUrlInvalid):
        normalize_feed_url(raw)


def test_the_feed_is_fetched_from_the_checked_address_without_a_token(feed):
    events = CanvasFeed(FEED_URL).events()
    assert [e.uid for e in events][:2] == ["event-assignment-101", "event-assignment-102"]
    [request] = feed.requests
    assert request.url.host == PUBLIC_IP
    assert request.url.path == f"/feeds/calendars/user_{CODE}.ics"
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert "Authorization" not in request.headers


@pytest.mark.parametrize("addresses", [["10.1.2.3"], ["127.0.0.1"], ["169.254.169.254"], [PUBLIC_IP, "192.168.0.1"]])
def test_a_feed_host_resolving_to_a_private_address_is_refused_before_any_request(client, db, feed, monkeypatch,
                                                                                 addresses):
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: addresses)
    response = client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    assert response.status_code == 422
    assert response.json()["detail"] == "canvas_url_invalid"
    assert feed.requests == []
    assert get_connection(db, USER_ID) is None


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404, text="Not Found"), CanvasFeedInvalid),
        (httpx.Response(401, text="Unauthorized"), CanvasFeedInvalid),
        (httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}), CanvasNotCanvas),
        (httpx.Response(200, text="<html>Log in to Canvas</html>"), CanvasNotCanvas),
        (httpx.Response(200, json={"errors": []}), CanvasNotCanvas),
    ],
)
def test_feed_answers_map_to_typed_errors(feed, response, error):
    feed.respond = lambda request: response
    with pytest.raises(error):
        CanvasFeed(FEED_URL).events()
    # Redirects are never followed.
    assert len(feed.requests) == 1


def test_an_oversized_feed_is_abandoned(feed, monkeypatch):
    monkeypatch.setattr(canvas_client, "MAX_RESPONSE_BYTES", 1000)
    feed.events = _feed_events() * 20
    with pytest.raises(CanvasNotCanvas):
        CanvasFeed(FEED_URL).events()


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parsing_reads_titles_courses_kinds_and_dates():
    tz_event = "\r\n".join([
        "BEGIN:VEVENT", "UID:event-calendar-event-7", "SUMMARY:Study group",
        "DTSTART;TZID=America/Los_Angeles:20261026T100000", "END:VEVENT",
    ])
    events = parse(_calendar(
        _event("event-assignment-101", "Problem Set 6 [CS 460-01 Fall 2026]", datetime(2026, 10, 24, 6, 59, tzinfo=UTC),
               _calendar_url(11, "assignment_101")),
        _event("event-assignment-102", "Essay\\, draft [COMM 103]", date(2026, 10, 25), all_day=True),
        tz_event,
        _event("event-sub-assignment-9", "Reply to two peers [PSY 101]", datetime(2026, 10, 27, tzinfo=UTC)),
        # Not Canvas's, untitled, or without a start: left out.
        _event("1234@example.com", "Imported", datetime(2026, 10, 27, tzinfo=UTC)),
        "BEGIN:VEVENT\r\nUID:event-assignment-5\r\nDTSTART:20261027T000000Z\r\nEND:VEVENT",
        "BEGIN:VEVENT\r\nUID:event-assignment-6\r\nSUMMARY:No date\r\nEND:VEVENT",
        "BEGIN:VEVENT\r\nUID:event-assignment-8\r\nSUMMARY:Bad date\r\nDTSTART:garbage\r\nEND:VEVENT",
    ))
    assert [(e.uid, e.kind, e.id) for e in events] == [
        ("event-assignment-101", "assignment", 101),
        ("event-assignment-102", "assignment", 102),
        ("event-calendar-event-7", "event", 7),
        ("event-sub-assignment-9", "other", 9),
    ]
    first, essay, study, reply = events
    assert (first.title, first.course_name, first.course_id) == ("Problem Set 6", "CS 460-01 Fall 2026", 11)
    assert first.start == datetime(2026, 10, 24, 6, 59, tzinfo=UTC)
    assert (essay.title, essay.course_name, essay.course_id, essay.start) == (
        "Essay, draft", "COMM 103", None, date(2026, 10, 25)
    )
    assert (study.title, study.course_name) == ("Study group", None)
    assert study.start == datetime(2026, 10, 26, 17, 0, tzinfo=UTC)
    assert reply.course_name == "PSY 101"


@pytest.mark.parametrize("body", [b"", b"<html>hi</html>", b'{"a": 1}', b"BEGIN:VEVENT\r\nEND:VEVENT\r\n"])
def test_text_that_is_not_a_calendar_is_refused(body):
    with pytest.raises(CanvasNotCanvas):
        parse(body)


# ── Connecting ───────────────────────────────────────────────────────────────

def test_connecting_a_feed_stores_only_ciphertext_and_returns_a_masked_status(client, db, feed):
    response = client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is True
    assert body["connection_type"] == "feed"
    assert body["status"] == "active"
    assert body["base_url"] == BASE_URL
    assert body["token_last4"] == "wxyz"
    assert body["feed_url_masked"] == f"{BASE_URL}/feeds/calendars/user_…wxyz.ics"
    assert body["canvas_user_id"] is None and body["canvas_user_name"] is None
    assert body["capabilities"] == {
        "upcoming": True, "missing": False, "recently_graded": False, "courses": False, "modules": False,
        "assignments": False, "announcements": False, "link_tasks": True, "submission_status": False,
    }
    assert CODE not in response.text and CODE[:-4] not in client.get("/canvas/connection").text
    assert len(feed.requests) == 1

    row = db.query(CanvasConnection).filter_by(user_id=USER_ID).one()
    assert row.connection_type == "feed"
    assert CODE.encode() not in row.token_ciphertext
    assert decrypt_token(row) == FEED_URL
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(
            crypto.Encrypted(ciphertext=row.token_ciphertext, nonce=row.token_nonce, key_version=row.key_version),
            "another-user",
        )


def test_the_token_connection_reports_its_type_and_every_capability(connected):
    body = connected.get("/canvas/connection").json()
    assert body["connection_type"] == "token"
    assert body["feed_url_masked"] is None
    assert all(body["capabilities"].values())


def test_not_connected_has_no_type_or_capabilities(client, feed):
    body = client.get("/canvas/connection").json()
    assert body["connected"] is False and body["connection_type"] is None
    assert not any(body["capabilities"].values())


@pytest.mark.parametrize(
    ("feed_url", "detail"),
    [
        (f"http://{HOST}/feeds/calendars/user_{CODE}.ics", "canvas_url_invalid"),
        (f"https://10.0.0.5/feeds/calendars/user_{CODE}.ics", "canvas_url_invalid"),
        (f"{BASE_URL}/courses/11", "canvas_feed_url_invalid"),
        (f"{BASE_URL}/feeds/calendars/user_{CODE}.ics" + "x" * 3000, "canvas_feed_url_invalid"),
    ],
)
def test_a_bad_feed_link_is_refused_without_fetching_or_echoing_it(client, db, feed, feed_url, detail):
    response = client.put("/canvas/connection/feed", json={"feed_url": feed_url})
    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert CODE not in response.text
    assert feed.requests == []
    assert get_connection(db, USER_ID) is None


def test_a_webcal_link_connects_as_https(client, db, feed):
    response = client.put("/canvas/connection/feed", json={"feed_url": f"webcal://{HOST}/feeds/calendars/user_{CODE}.ics"})
    assert response.status_code == 200, response.text
    assert response.json()["feed_url_masked"] == f"{BASE_URL}/feeds/calendars/user_…wxyz.ics"
    [request] = feed.requests
    assert request.url.scheme == "https" and request.url.host == PUBLIC_IP
    assert decrypt_token(get_connection(db, USER_ID)) == FEED_URL


def test_a_webcal_link_to_a_private_address_is_refused(client, feed, monkeypatch):
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: ["127.0.0.1"])
    response = client.put("/canvas/connection/feed", json={"feed_url": f"webcal://{HOST}/feeds/calendars/user_{CODE}.ics"})
    assert response.status_code == 422
    assert response.json() == {"detail": "canvas_url_invalid"}
    assert feed.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"feed_url": {"url": FEED_URL}},
        {"feed_url": [FEED_URL]},
        {"feed_url": 42, "note": FEED_URL},
        {"feed_url": FEED_URL, "allow_account_change": FEED_URL},
        {"url": FEED_URL},
        FEED_URL,
        [FEED_URL],
    ],
)
def test_a_malformed_body_never_echoes_the_link(client, db, feed, caplog, body):
    caplog.set_level(logging.DEBUG)
    response = client.put("/canvas/connection/feed", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "canvas_feed_url_invalid"}
    assert CODE[-12:] not in response.text and CODE[-12:] not in caplog.text
    assert feed.requests == []
    assert get_connection(db, USER_ID) is None


def test_rejected_links_are_never_echoed(client, feed, caplog):
    caplog.set_level(logging.DEBUG)
    for feed_url in [
        f"http://{HOST}/feeds/calendars/user_{CODE}.ics",
        f"https://10.0.0.5/feeds/calendars/user_{CODE}.ics",
        f"{BASE_URL}/feeds/calendars/user_{CODE}.ics?x=1",
        f"{BASE_URL}/feeds/calendars/user_{CODE}.ics" + "x" * 3000,
        f"{BASE_URL}/feeds/calendars/user_{NEW_CODE[:-1]}Q.ics",
    ]:
        response = client.put("/canvas/connection/feed", json={"feed_url": feed_url})
        assert response.status_code == 422
        assert set(response.json()) == {"detail"} and isinstance(response.json()["detail"], str)
        assert CODE[-12:] not in response.text and NEW_CODE[-12:-1] not in response.text
    assert CODE[-12:] not in caplog.text and NEW_CODE[-12:-1] not in caplog.text


def test_a_feed_canvas_does_not_know_is_not_stored(client, db, feed):
    response = client.put(
        "/canvas/connection/feed", json={"feed_url": f"{BASE_URL}/feeds/calendars/user_{'Q' * 40}.ics"}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "canvas_feed_invalid"
    assert get_connection(db, USER_ID) is None


def test_a_host_that_does_not_serve_a_calendar_is_refused(client, db, feed):
    feed.respond = lambda request: httpx.Response(200, text="<html>Welcome</html>")
    response = client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    assert response.status_code == 422
    assert response.json()["detail"] == "canvas_not_canvas"
    assert get_connection(db, USER_ID) is None


def test_a_feed_replaces_a_token_for_the_same_school_and_keeps_settings(connected, db, feed):
    assert connected.patch("/canvas/connection", json={"hidden_course_ids": [12]}).status_code == 200
    body = connected.put("/canvas/connection/feed", json={"feed_url": FEED_URL}).json()
    assert body["connection_type"] == "feed"
    assert body["hidden_course_ids"] == [12]
    # And a token replaces the feed again without confirmation.
    body = connected.put("/canvas/connection", json={"base_url": BASE_URL, "token": TOKEN}).json()
    assert body["connection_type"] == "token"
    assert body["canvas_user_id"] == 42
    assert body["feed_url_masked"] is None
    assert body["hidden_course_ids"] == [12]


def test_a_feed_for_another_school_needs_confirmation_and_resets_settings(feed_connected, feed, canvas):
    feed_connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    other = f"https://canvas.ucsd.edu/feeds/calendars/user_{NEW_CODE}.ics"
    response = feed_connected.put("/canvas/connection/feed", json={"feed_url": other})
    assert response.status_code == 409
    assert response.json()["detail"] == "canvas_account_changed"
    assert feed_connected.get("/canvas/connection").json()["base_url"] == BASE_URL

    body = feed_connected.put("/canvas/connection/feed", json={"feed_url": other, "allow_account_change": True}).json()
    assert body["base_url"] == "https://canvas.ucsd.edu"
    assert body["hidden_course_ids"] == []


def test_test_refetches_the_feed_and_marks_a_reset_one_invalid(feed_connected, feed):
    body = feed_connected.post("/canvas/connection/test").json()
    assert body["status"] == "active"
    assert len(feed.requests) == 1

    feed.codes.discard(CODE)
    body = feed_connected.post("/canvas/connection/test").json()
    assert body["status"] == "invalid"
    response = feed_connected.get("/canvas/overview")
    assert response.status_code == 409
    assert response.json()["detail"] == "canvas_feed_invalid"


def test_canvas_down_during_a_feed_test_marks_an_error(feed_connected, feed):
    feed.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    response = feed_connected.post("/canvas/connection/test")
    assert response.status_code == 502
    assert feed_connected.get("/canvas/connection").json()["status"] == "error"


def test_removing_a_feed_connection_deletes_it(feed_connected, db):
    assert feed_connected.delete("/canvas/connection").status_code == 204
    assert get_connection(db, USER_ID) is None


def test_demo_sandboxes_cannot_connect_a_feed(demo_client, db, feed):
    response = demo_client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    assert response.status_code == 403
    assert feed.requests == []
    assert get_connection(db, USER_ID) is None


def test_the_feed_link_never_appears_in_logs(client, feed, caplog):
    caplog.set_level(logging.DEBUG)
    client.put("/canvas/connection/feed", json={"feed_url": FEED_URL})
    client.get("/canvas/connection")
    client.post("/canvas/connection/test")
    client.get("/canvas/overview", params={"refresh": True})
    feed.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    client.post("/canvas/connection/test")
    assert any("HTTP Request" in r.getMessage() for r in caplog.records)
    assert CODE not in caplog.text and CODE[-12:] not in caplog.text


def test_the_feed_repr_hides_the_link():
    assert CODE not in repr(CanvasFeed(FEED_URL))


# ── The panel ────────────────────────────────────────────────────────────────

def test_the_overview_lists_the_feeds_next_week(feed_connected, feed):
    body = _overview(feed_connected)
    assert body["missing"] == [] and body["recently_graded"] == []
    assert body["sample"] is False and body["stale"] is False
    upcoming = body["upcoming"]
    # Dentist (SOON) and Problem Set 6 share a due time; ids break the tie.
    assert [i["uid"] for i in upcoming] == [
        "event-calendar-event-56", "event-assignment-101", "event-assignment-102", "event-calendar-event-55",
    ]
    dentist, problem_set, speech, office_hours = upcoming
    assert problem_set == {
        "id": 101, "kind": "assignment", "title": "Problem Set 6", "course_id": 11,
        "course_name": "CS 460-01 Fall 2026", "due_at": problem_set["due_at"], "points_possible": None,
        "html_url": f"{BASE_URL}/courses/11/assignments/101", "submission": None,
        "uid": "event-assignment-101", "all_day": False,
    }
    assert datetime.fromisoformat(problem_set["due_at"]) == SOON.replace(microsecond=0)
    # An all-day assignment is due at 11:59 pm in the caller's time zone.
    assert speech["all_day"] is True
    assert datetime.fromisoformat(speech["due_at"]) == datetime.combine(ALL_DAY, datetime.min.time().replace(
        hour=23, minute=59), tzinfo=LA)
    assert office_hours["kind"] == "event"
    assert office_hours["html_url"] == f"{BASE_URL}/calendar?include_contexts=course_11&event_id=55"
    # A link off the school's host is dropped.
    assert dentist["html_url"] is None and dentist["course_id"] is None
    assert len(feed.requests) == 1


def test_the_feed_is_cached_and_hidden_courses_are_left_out(feed_connected, feed):
    _overview(feed_connected)
    feed_connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    body = _overview(feed_connected)
    assert "event-assignment-102" not in [i["uid"] for i in body["upcoming"]]
    assert len(feed.requests) == 1


def test_canvas_down_serves_the_cached_feed_marked_stale(feed_connected, feed, monkeypatch):
    first = _overview(feed_connected)
    feed.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    later = datetime.now(UTC) + timedelta(minutes=20)
    monkeypatch.setattr("app.routers.canvas_router.datetime", type("D", (datetime,), {"now": staticmethod(
        lambda tz=None: later)}))
    body = _overview(feed_connected)
    assert body["stale"] is True
    assert [i["uid"] for i in body["upcoming"]] == [i["uid"] for i in first["upcoming"]]
    assert len(feed.requests) == 2


@pytest.mark.parametrize(
    "path",
    ["/canvas/courses", "/canvas/courses/11/modules", "/canvas/courses/11/assignments", "/canvas/announcements"],
)
def test_token_only_routes_are_not_supported_for_a_feed(feed_connected, feed, canvas, path):
    canvas.requests.clear()
    response = feed_connected.get(path)
    assert response.status_code == 404
    assert response.json()["detail"] == "canvas_not_supported"
    assert canvas.requests == []


# ── Tasks added from feed items ──────────────────────────────────────────────

def _add(client, item: dict, **fields):
    body = {"title": item["title"], "canvas_uid": item["uid"], "canvas_html_url": item["html_url"],
            "canvas_due_at": item["due_at"], **fields}
    if item["kind"] == "assignment":
        body["canvas_assignment_id"] = item["id"]
    return client.post("/create-task", json=body)


def _sync(client):
    response = client.post("/canvas/linked-tasks/sync", params={"tz": "America/Los_Angeles"})
    assert response.status_code == 200, response.text
    return response.json()


def _task(db, task_id) -> Task:
    db.expire_all()
    return db.get(Task, task_id)


def test_feed_items_can_be_added_to_tasks_once(feed_connected):
    by_uid = {i["uid"]: i for i in _overview(feed_connected)["upcoming"]}
    event = by_uid["event-calendar-event-55"]
    response = _add(feed_connected, event)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["canvas_uid"] == "event-calendar-event-55"
    assert task["canvas_assignment_id"] is None
    assert task["canvas_html_url"] == event["html_url"]

    again = _add(feed_connected, event)
    assert again.status_code == 409
    assert again.json()["detail"] == {
        "code": "canvas_task_exists", "message": "An open task already follows this Canvas assignment.",
        "task_id": task["id"],
    }
    # An assignment already followed by id (e.g. added while a token was connected) is taken too.
    followed = feed_connected.post("/create-task", json={"title": "PS6", "canvas_assignment_id": 101})
    assert followed.status_code == 200
    assert _add(feed_connected, by_uid["event-assignment-101"]).status_code == 409


def test_a_task_relinked_to_another_feed_item_is_checked(feed_connected):
    first = _add(feed_connected, {"title": "x", "uid": "event-calendar-event-55", "html_url": None, "due_at": None,
                                  "kind": "event"}).json()
    other = feed_connected.post("/create-task", json={"title": "y"}).json()
    response = feed_connected.put(f"/update-task/{other['id']}", json={"title": "y", "canvas_uid": first["canvas_uid"]})
    assert response.status_code == 409
    # Saving without the link fields keeps the link.
    saved = feed_connected.put(f"/update-task/{first['id']}", json={"title": "renamed"}).json()
    assert saved["canvas_uid"] == "event-calendar-event-55"
    assert feed_connected.put(f"/update-task/{first['id']}", json={"title": "x", "canvas_uid": None}).json()[
        "canvas_uid"] is None


def test_sync_follows_the_feeds_due_dates_without_submission_status(feed_connected, db, feed):
    by_uid = {i["uid"]: i for i in _overview(feed_connected)["upcoming"]}
    added = _add(feed_connected, by_uid["event-assignment-101"]).json()
    event = _add(feed_connected, by_uid["event-calendar-event-55"]).json()
    by_id = feed_connected.post("/create-task", json={"title": "Old", "canvas_assignment_id": 103}).json()
    gone = feed_connected.post("/create-task", json={"title": "Gone", "canvas_uid": "event-assignment-999"}).json()

    moved = SOON + timedelta(days=1, hours=3)
    feed.events = [e.replace(_ics_time(SOON), _ics_time(moved)) if "assignment-101" in e else e for e in feed.events]
    body = _sync(feed_connected)
    # The feed was cached by the overview, so the change isn't seen yet.
    assert {r["task_id"]: r["due_changed"] for r in body["results"]}[added["id"]] is False

    canvas_cache.clear()
    results = {r["task_id"]: r for r in _sync(feed_connected)["results"]}
    assert list(results) == sorted([added["id"], event["id"], by_id["id"], gone["id"]])
    assert results[added["id"]]["synced"] is True
    assert results[added["id"]]["submitted"] is False
    assert results[added["id"]]["due_changed"] is True
    local = moved.astimezone(LA)
    task = _task(db, added["id"])
    assert task.due_date.date() == local.date()
    assert task.due_time.strftime("%H:%M") == local.strftime("%H:%M")
    assert results[event["id"]]["synced"] is True and results[event["id"]]["due_changed"] is False
    # A task following an assignment by id alone matches its feed item.
    assert results[by_id["id"]]["synced"] is True
    assert datetime.fromisoformat(results[by_id["id"]]["due_at"]) == FAR.replace(microsecond=0)
    assert results[gone["id"]] == {"task_id": gone["id"], "synced": False, "submitted": False, "due_at": None,
                                   "due_changed": False}


def test_sync_without_linked_tasks_does_not_fetch_the_feed(feed_connected, feed):
    assert _sync(feed_connected)["results"] == []
    assert feed.requests == []


def test_token_sync_ignores_tasks_that_only_follow_a_feed_item(connected, canvas):
    connected.post("/create-task", json={"title": "x", "canvas_uid": "event-calendar-event-55"})
    body = connected.post("/canvas/linked-tasks/sync", params={"tz": "UTC"}).json()
    assert body["results"] == []
    assert canvas.requests == []
