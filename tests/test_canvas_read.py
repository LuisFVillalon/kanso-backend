"""Canvas panel data: pagination, backoff, the cache, an invalid token, hidden courses and demo sample data."""

import base64
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import canvas_cache, canvas_client, crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import CanvasClient, CanvasForbidden, CanvasNotCanvas, CanvasRateLimited
from app.core.rate_limit import RateLimiter
from app.crud import canvas_data_crud
from app.crud.canvas_crud import delete_connection, get_connection
from app.crud.canvas_data_crud import canvas_url, plain_text
from app.database.database import get_db
from app.main import app
from app.routers import canvas_router
from tests.conftest import USER_ID
from tests.test_canvas import _over_the_network

BASE_URL = "https://sdsu.instructure.com"
HOST = "sdsu.instructure.com"
PUBLIC_IP = "151.101.1.1"
TOKEN = "7~Hq2aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3d7Hq2"
NEW_TOKEN = "7~Zz9aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3dR4nd"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(UTC)
SOON = _iso(NOW + timedelta(days=2))
LATER = _iso(NOW + timedelta(days=4))
PAST = _iso(NOW - timedelta(days=3))
FAR = _iso(NOW + timedelta(days=30))


def _planner(item_id, course_id, title, due, kind="assignment", submissions=None, **plannable):
    return {
        "plannable_id": str(item_id),
        "plannable_type": kind,
        "course_id": course_id,
        "context_name": f"Course {course_id}",
        "html_url": f"/courses/{course_id}/assignments/{item_id}",
        "plannable_date": due,
        "plannable": {"title": title, "due_at": due, "points_possible": 10, **plannable},
        "submissions": submissions if submissions is not None else {"submitted": False, "late": False},
    }


COURSES = [
    {
        "id": 11, "name": "CS 460: Algorithms", "course_code": "CS 460", "term": {"name": "Fall 2026"},
        "enrollments": [{"type": "student", "computed_current_score": 91.4, "computed_current_grade": "A-",
                         "current_grading_period_title": "Fall", "current_period_computed_current_score": 90.0,
                         "current_period_computed_current_grade": "A-"}],
    },
    {
        "id": 12, "name": "COMM 103", "course_code": "COMM 103", "hide_final_grades": True,
        "enrollments": [{"type": "student", "computed_current_score": 70.0, "computed_current_grade": "C"}],
    },
    {"id": 13, "access_restricted_by_date": True},
    {"id": 14, "name": "PSY 101", "course_code": "PSY 101", "enrollments": [{"type": "student"}]},
]


class CanvasAPI:
    """A school's Canvas, serving lists in pages with Link headers like the real one."""

    def __init__(self):
        self.valid = {TOKEN: {"id": 42, "name": "Ada Lovelace"}, NEW_TOKEN: {"id": 42, "name": "Ada Lovelace"}}
        self.requests: list[httpx.Request] = []
        # path -> pages, each a list of items
        self.lists: dict[str, list[list]] = {
            "/api/v1/planner/items": [[
                _planner(101, 11, "Problem Set 6", SOON),
                _planner(102, 12, "Speech outline", LATER, submissions={"submitted": True, "late": True}),
                _planner(103, 14, "Quiz 4", SOON, kind="quiz"),
                _planner(104, 11, "Way later", FAR),
            ]],
            "/api/v1/users/self/missing_submissions": [[
                {"id": 201, "name": "Reflection paper", "due_at": PAST, "course_id": 14, "points_possible": 20,
                 "html_url": f"{BASE_URL}/courses/14/assignments/201", "course": {"course_code": "PSY 101"}},
                {"id": 202, "name": "Hidden course work", "due_at": PAST, "course_id": 12,
                 "submission_types": ["online_quiz"]},
            ]],
            "/api/v1/courses": [COURSES],
            "/api/v1/courses/11/modules": [[
                {"id": 1, "name": "Week 1", "position": 1, "state": "completed", "items": [
                    {"id": 10, "title": "Syllabus", "type": "Page",
                     "html_url": f"{BASE_URL}/courses/11/modules/items/10",
                     "completion_requirement": {"type": "must_view", "completed": True}},
                ]},
                {"id": 2, "name": "Week 2 (long)", "position": 2, "state": "locked", "items_count": 2,
                 "unlock_at": FAR, "require_sequential_progress": True},
            ]],
            "/api/v1/courses/11/modules/2/items": [[
                {"id": 20, "title": "Lab", "type": "Assignment", "indent": 1,
                 "content_details": {"locked_for_user": True, "due_at": FAR, "points_possible": 40},
                 "completion_requirement": {"type": "min_score", "min_score": 30}},
                {"id": 21, "title": "Slides", "type": "File", "html_url": "javascript:alert(1)"},
            ]],
            "/api/v1/courses/11/assignments": [[
                {"id": 301, "name": "Problem Set 5", "due_at": PAST, "points_possible": 50,
                 "html_url": f"{BASE_URL}/courses/11/assignments/301", "submission_types": ["online_upload"],
                 "submission": {"workflow_state": "graded", "submitted_at": PAST, "score": 47, "grade": "47",
                                "graded_at": PAST, "late": False, "missing": False}},
                {"id": 302, "name": "Quiz 3", "due_at": SOON, "submission_types": ["online_quiz"], "quiz_id": 9,
                 "submission": {"workflow_state": "unsubmitted"}},
                {"id": 303, "name": "New Quiz", "due_at": LATER, "is_quiz_assignment": True,
                 "submission_types": ["external_tool"]},
            ]],
            "/api/v1/announcements": [[
                {"id": 401, "title": "Office hours moved", "message": "<p>Thursday <b>2&ndash;4 pm</b></p>"
                 "<script>steal()</script><p>GMCS&nbsp;405</p>", "posted_at": PAST, "context_code": "course_11",
                 "author": {"display_name": "Dr. Rivera"}, "read_state": "unread",
                 "html_url": f"{BASE_URL}/courses/11/discussion_topics/401"},
                {"id": 402, "title": "Hidden course news", "message": "x", "posted_at": PAST,
                 "context_code": "course_12", "html_url": "https://evil.example.com/phish"},
            ]],
        }
        # Set to a callable to answer a request differently; return None to fall through.
        self.respond = None

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.respond:
            response = self.respond(request)
            if response is not None:
                return response
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(
                401, json={"errors": [{"message": "Invalid access token."}]},
                headers={"WWW-Authenticate": 'Bearer realm="canvas-lms"'},
            )
        path = request.url.path
        if path == "/api/v1/users/self":
            return httpx.Response(200, json=self.valid[token])
        if path == "/api/v1/users/self/user_generated_tokens":
            return httpx.Response(200, json=[])
        if path not in self.lists:
            return httpx.Response(404, json={"errors": [{"message": "The specified resource does not exist."}]})
        pages = self.lists[path]
        page = int(request.url.params.get("page", "1"))
        links = [f'<https://{HOST}{path}?page=1&per_page=100>; rel="first"']
        if page < len(pages):
            links.append(f'<https://{HOST}{path}?page={page + 1}&per_page=100>; rel="next"')
        return httpx.Response(200, json=pages[page - 1], headers={"Link": ", ".join(links)})


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """A fresh key, an empty cache, open rate limits and no real waiting, for every test."""
    for name in list(os.environ):
        if name.startswith(crypto.KEY_ENV):
            monkeypatch.delenv(name)
    monkeypatch.setenv(crypto.KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    canvas_cache.clear()
    monkeypatch.setattr(canvas_data_crud, "_fetch_limiter", RateLimiter(max_calls=1000, period=60))
    monkeypatch.setattr(canvas_router, "_canvas_call_limiter", RateLimiter(max_calls=1000, period=60))
    yield
    canvas_cache.clear()


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(canvas_client, "_sleep", waited.append)
    return waited


@pytest.fixture
def canvas(monkeypatch, sleeps):
    fake = CanvasAPI()
    monkeypatch.setattr(canvas_client, "transport", httpx.MockTransport(_over_the_network(fake)))
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: [PUBLIC_IP])
    return fake


@pytest.fixture
def connected(client, canvas):
    """client, connected to the fake Canvas, with the connect calls forgotten."""
    response = client.put("/canvas/connection", json={"base_url": BASE_URL, "token": TOKEN})
    assert response.status_code == 200
    canvas.requests.clear()
    return client


@pytest.fixture
def demo_client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=USER_ID, email="", provider="anonymous", is_anonymous=True
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Pagination ───────────────────────────────────────────────────────────────

def test_get_list_follows_next_links_on_the_pinned_address(canvas):
    canvas.lists["/api/v1/courses"] = [[{"id": 1}], [{"id": 2}], [{"id": 3}]]
    assert CanvasClient(BASE_URL, TOKEN).get_list("/courses", {"per_page": 100}) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [r.url.params.get("page") for r in canvas.requests] == [None, "2", "3"]
    assert {r.url.host for r in canvas.requests} == {PUBLIC_IP}
    assert all(r.headers["Host"] == HOST and r.extensions["sni_hostname"] == HOST for r in canvas.requests)


def test_get_list_stops_at_the_page_cap(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "MAX_PAGES", 3)
    canvas.lists["/api/v1/courses"] = [[{"id": n}] for n in range(50)]
    assert len(CanvasClient(BASE_URL, TOKEN).get_list("/courses")) == 3
    assert len(canvas.requests) == 3


def test_link_headers_are_read_case_insensitively(canvas):
    def respond(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2}])
        return httpx.Response(
            200, json=[{"id": 1}],
            headers={"link": f'<https://{HOST}/api/v1/courses?page=2>; REL=NEXT, <https://{HOST}/x>; rel="last"'},
        )

    canvas.respond = respond
    assert CanvasClient(BASE_URL, TOKEN).get_list("/courses") == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.com/api/v1/courses?page=2",
        "http://sdsu.instructure.com/api/v1/courses?page=2",
        "https://sdsu.instructure.com:8443/api/v1/courses?page=2",
        "https://user@sdsu.instructure.com/api/v1/courses?page=2",
        "https://sdsu.instructure.com/login?page=2",
    ],
)
def test_a_next_link_leaving_the_school_api_is_never_followed(canvas, target):
    canvas.respond = lambda request: httpx.Response(200, json=[{"id": 1}], headers={"Link": f'<{target}>; rel="next"'})
    with pytest.raises(CanvasNotCanvas):
        CanvasClient(BASE_URL, TOKEN).get_list("/courses")
    assert len(canvas.requests) == 1


def test_a_page_that_is_not_a_list_is_refused(canvas):
    canvas.respond = lambda request: httpx.Response(200, json={"courses": []})
    with pytest.raises(CanvasNotCanvas):
        CanvasClient(BASE_URL, TOKEN).get_list("/courses")


# ── Rate limits ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "throttled",
    [httpx.Response(429, text="429 Forbidden (Rate Limit Exceeded)"),
     httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)")],
)
def test_a_throttled_request_backs_off_and_retries(canvas, sleeps, throttled):
    answers = iter([throttled, throttled])
    canvas.respond = lambda request: next(answers, None)
    assert CanvasClient(BASE_URL, TOKEN).get_list("/courses") == COURSES
    assert len(canvas.requests) == 3
    assert len(sleeps) == 2
    assert 1 <= sleeps[0] <= 1.5 and 2 <= sleeps[1] <= 3


def test_backoff_gives_up_after_three_retries(canvas, sleeps):
    canvas.respond = lambda request: httpx.Response(429, text="Rate Limit Exceeded")
    with pytest.raises(CanvasRateLimited):
        CanvasClient(BASE_URL, TOKEN).get_list("/courses")
    assert len(canvas.requests) == 4
    assert len(sleeps) == 3


def test_a_low_quota_slows_the_next_page(canvas, sleeps):
    def respond(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[], headers={
            "Link": f'<https://{HOST}/api/v1/courses?page=2>; rel="next"', "X-Rate-Limit-Remaining": "12.5"})

    canvas.respond = respond
    CanvasClient(BASE_URL, TOKEN).get_list("/courses")
    assert sleeps == [canvas_client.LOW_QUOTA_PAUSE]


def test_a_permission_refusal_401_is_not_an_invalid_token(canvas):
    canvas.respond = lambda request: httpx.Response(
        401, json={"status": "unauthorized", "errors": [{"message": "user not authorized to perform that action"}]}
    )
    with pytest.raises(CanvasForbidden):
        CanvasClient(BASE_URL, TOKEN).get_list("/courses/11/modules")


# ── Overview ─────────────────────────────────────────────────────────────────

def test_overview_lists_the_next_week_and_missing_work(connected, canvas):
    response = connected.get("/canvas/overview", params={"tz": "America/Los_Angeles"})
    assert response.status_code == 200
    body = response.json()
    assert body["busy"] is False and body["stale"] is False and body["sample"] is False
    assert datetime.fromisoformat(body["fetched_at"]) <= datetime.now(UTC)
    assert [i["id"] for i in body["upcoming"]] == [101, 103, 102]
    first = body["upcoming"][0]
    assert first == {
        "id": 101, "kind": "assignment", "title": "Problem Set 6", "course_id": 11, "course_name": "Course 11",
        "due_at": first["due_at"], "points_possible": 10.0, "html_url": f"{BASE_URL}/courses/11/assignments/101",
        "submission": {"submitted": False, "missing": False, "late": False, "graded": False, "excused": False,
                       "score": None, "grade": None, "submitted_at": None, "graded_at": None},
    }
    assert body["upcoming"][1]["kind"] == "quiz"
    assert body["upcoming"][2]["submission"]["late"] is True
    assert [i["id"] for i in body["missing"]] == [201, 202]
    assert body["missing"][0]["course_name"] == "PSY 101"
    assert body["missing"][1]["kind"] == "quiz"

    planner = next(r for r in canvas.requests if r.url.path == "/api/v1/planner/items")
    start = datetime.fromisoformat(body["start"])
    assert start.utcoffset() == datetime.now(ZoneInfo("America/Los_Angeles")).utcoffset()
    assert (start.hour, start.minute) == (0, 0)
    assert datetime.fromisoformat(body["end"]) - start == timedelta(days=7)
    assert datetime.fromisoformat(planner.url.params["start_date"]) == start - timedelta(days=7)
    assert datetime.fromisoformat(planner.url.params["end_date"]) == datetime.fromisoformat(body["end"])
    missing = next(r for r in canvas.requests if r.url.path.endswith("/missing_submissions"))
    assert missing.url.params.get_list("filter[]") == ["submittable", "current_grading_period"]


def _graded_last_week():
    return [
        _planner(110, 11, "Problem Set 4", _iso(NOW - timedelta(days=8, hours=12)), submissions={"graded": True}),
        _planner(111, 11, "Problem Set 5", _iso(NOW - timedelta(days=5)), submissions={"graded": True}),
        _planner(112, 14, "Quiz 3", PAST, kind="quiz", submissions={"submitted": True, "graded": True}),
        _planner(113, 12, "Speech draft", PAST, submissions={"graded": True}),
        _planner(114, 14, "Ungraded essay", PAST, submissions={"submitted": True}),
    ]


def test_overview_lists_recently_graded_work_newest_first(connected, canvas):
    canvas.lists["/api/v1/planner/items"][0] += _graded_last_week()
    body = connected.get("/canvas/overview").json()
    assert [i["id"] for i in body["recently_graded"]] == [113, 112, 111]
    assert body["recently_graded"][1]["kind"] == "quiz"
    assert [i["id"] for i in body["upcoming"]] == [101, 103, 102]
    assert canvas.paths().count("/api/v1/planner/items") == 1


def test_recently_graded_is_capped(connected, canvas):
    canvas.lists["/api/v1/planner/items"] = [[
        _planner(500 + n, 11, f"Exercise {n}", _iso(NOW - timedelta(days=3, minutes=n)), submissions={"graded": True})
        for n in range(15)
    ]]
    graded = connected.get("/canvas/overview").json()["recently_graded"]
    assert [i["id"] for i in graded] == list(range(500, 510))


def test_overview_leaves_out_hidden_courses(connected, canvas):
    canvas.lists["/api/v1/planner/items"][0] += _graded_last_week()
    connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    body = connected.get("/canvas/overview").json()
    assert 102 not in [i["id"] for i in body["upcoming"]]
    assert [i["id"] for i in body["missing"]] == [201]
    assert [i["id"] for i in body["recently_graded"]] == [112, 111]


def test_a_missing_canvas_route_on_the_overview_is_not_a_missing_course(connected, canvas):
    del canvas.lists["/api/v1/planner/items"]
    response = connected.get("/canvas/overview")
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_not_canvas")


def test_an_unknown_time_zone_is_refused(connected, canvas):
    assert connected.get("/canvas/overview", params={"tz": "Mars/Olympus"}).status_code == 422
    assert canvas.requests == []


# ── Courses, modules, assignments, announcements ─────────────────────────────

def test_courses_come_with_grades_and_respect_hidden_grades(connected, canvas):
    body = connected.get("/canvas/courses").json()
    courses = {c["id"]: c for c in body["courses"]}
    assert list(courses) == [11, 12, 14]
    assert courses[11] == {
        "id": 11, "name": "CS 460: Algorithms", "course_code": "CS 460", "term": "Fall 2026",
        "html_url": f"{BASE_URL}/courses/11", "grades_hidden": False, "current_score": 91.4, "current_grade": "A-",
        "grading_period": "Fall", "period_score": 90.0, "period_grade": "A-", "hidden": False,
    }
    assert courses[12]["grades_hidden"] is True
    assert courses[12]["current_score"] is None and courses[12]["current_grade"] is None
    assert courses[14]["current_score"] is None
    request = canvas.requests[0]
    assert request.url.params["enrollment_state"] == "active"
    assert request.url.params.get_list("include[]") == ["total_scores", "current_grading_period_scores", "term"]


def test_hidden_courses_are_left_out_unless_asked_for(connected, canvas):
    connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    assert [c["id"] for c in connected.get("/canvas/courses").json()["courses"]] == [11, 14]
    everything = connected.get("/canvas/courses", params={"include_hidden": "true"}).json()["courses"]
    assert [(c["id"], c["hidden"]) for c in everything] == [(11, False), (12, True), (14, False)]
    assert len(canvas.requests) == 1


def test_modules_fetch_long_modules_items_and_drop_unsafe_links(connected, canvas):
    body = connected.get("/canvas/courses/11/modules").json()
    assert body["course_id"] == 11
    week1, week2 = body["modules"]
    assert week1["state"] == "completed"
    assert week1["items"][0]["requirement"] == {"type": "must_view", "min_score": None, "completed": True}
    assert week2["state"] == "locked" and week2["require_sequential_progress"] is True
    assert week2["items_complete"] is True
    lab, slides = week2["items"]
    assert lab["locked"] is True and lab["points_possible"] == 40 and lab["indent"] == 1
    assert lab["requirement"]["min_score"] == 30
    assert slides["html_url"] is None
    assert canvas.paths() == ["/api/v1/courses/11/modules", "/api/v1/courses/11/modules/2/items"]


def test_assignments_include_quizzes_and_the_users_submission(connected, canvas):
    body = connected.get("/canvas/courses/11/assignments").json()
    ps5, quiz3, new_quiz = body["assignments"]
    assert ps5["kind"] == "assignment"
    assert ps5["submission"]["graded"] is True and ps5["submission"]["submitted"] is True
    assert ps5["submission"]["score"] == 47
    assert (quiz3["kind"], quiz3["quiz_id"]) == ("quiz", 9)
    assert quiz3["submission"]["submitted"] is False
    assert new_quiz["kind"] == "quiz" and new_quiz["submission"] is None
    assert canvas.requests[0].url.params["order_by"] == "due_at"


def test_announcements_cover_the_users_courses_as_plain_text(connected, canvas):
    connected.patch("/canvas/connection", json={"hidden_course_ids": [12]})
    body = connected.get("/canvas/announcements").json()
    [announcement] = body["announcements"]
    assert announcement["message"] == "Thursday 2–4 pm\n\nGMCS 405"
    assert announcement["author"] == "Dr. Rivera"
    assert announcement["read"] is False
    assert announcement["course_id"] == 11
    request = next(r for r in canvas.requests if r.url.path == "/api/v1/announcements")
    assert request.url.params.get_list("context_codes[]") == ["course_11", "course_12", "course_14"]


def test_announcement_links_off_the_school_host_are_dropped(connected, canvas):
    announcements = connected.get("/canvas/announcements").json()["announcements"]
    assert {a["id"]: a["html_url"] for a in announcements}[402] is None


def test_no_courses_means_no_announcement_call(connected, canvas):
    canvas.lists["/api/v1/courses"] = [[]]
    assert connected.get("/canvas/announcements").json()["announcements"] == []
    assert canvas.paths() == ["/api/v1/courses"]


def test_a_hidden_course_is_not_fetched(connected, canvas):
    connected.patch("/canvas/connection", json={"hidden_course_ids": [11]})
    for path in ("/canvas/courses/11/modules", "/canvas/courses/11/assignments"):
        response = connected.get(path)
        assert (response.status_code, response.json()["detail"]) == (404, "canvas_course_hidden")
    assert canvas.requests == []


def test_an_unknown_course_is_not_found(connected, canvas):
    response = connected.get("/canvas/courses/999/modules")
    assert (response.status_code, response.json()["detail"]) == (404, "canvas_course_not_found")


def test_a_course_refusing_its_modules_keeps_the_connection_active(connected, db, canvas):
    canvas.respond = lambda request: httpx.Response(
        401, json={"status": "unauthorized", "errors": [{"message": "user not authorized to perform that action"}]}
    )
    response = connected.get("/canvas/courses/11/modules")
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_forbidden")
    assert get_connection(db, USER_ID).status == "active"


def test_reads_without_a_connection_are_not_found(client, canvas):
    response = client.get("/canvas/overview")
    assert (response.status_code, response.json()["detail"]) == (404, "canvas_not_connected")
    assert canvas.requests == []


# ── Cache ────────────────────────────────────────────────────────────────────

def test_a_fresh_cache_is_served_without_calling_canvas(connected, db, canvas):
    first = connected.get("/canvas/courses").json()
    second = connected.get("/canvas/courses").json()
    assert first == second
    assert len(canvas.requests) == 1
    assert get_connection(db, USER_ID).last_synced_at is not None


def test_refresh_refetches_but_not_more_than_every_30_seconds(connected, canvas, monkeypatch):
    connected.get("/canvas/courses")
    connected.get("/canvas/courses", params={"refresh": "true"})
    assert len(canvas.requests) == 1
    monkeypatch.setattr(canvas_data_crud, "MIN_REFRESH_AGE", timedelta(0))
    connected.get("/canvas/courses", params={"refresh": "true"})
    assert len(canvas.requests) == 2


def test_stale_data_is_refetched(connected, canvas, monkeypatch):
    connected.get("/canvas/courses")
    monkeypatch.setitem(canvas_data_crud.TTL, "courses", timedelta(0))
    connected.get("/canvas/courses")
    assert len(canvas.requests) == 2


def test_rate_limited_canvas_serves_the_cache_marked_busy(connected, canvas, sleeps, monkeypatch):
    fresh = connected.get("/canvas/courses").json()
    monkeypatch.setitem(canvas_data_crud.TTL, "courses", timedelta(0))
    canvas.respond = lambda request: httpx.Response(429, text="429 Forbidden (Rate Limit Exceeded)")
    response = connected.get("/canvas/courses")
    assert response.status_code == 200
    body = response.json()
    assert body["busy"] is True and body["stale"] is True
    assert body["courses"] == fresh["courses"]
    assert body["fetched_at"] == fresh["fetched_at"]
    assert len(sleeps) == 3


def test_rate_limited_canvas_with_nothing_cached_is_busy(connected, canvas, sleeps):
    canvas.respond = lambda request: httpx.Response(429, text="Rate Limit Exceeded")
    response = connected.get("/canvas/courses")
    assert (response.status_code, response.json()["detail"]) == (502, "canvas_busy")
    assert response.headers["Retry-After"] == "30"


def test_unreachable_canvas_serves_the_cache_marked_stale(connected, canvas, monkeypatch):
    connected.get("/canvas/overview")
    monkeypatch.setitem(canvas_data_crud.TTL, "overview", timedelta(0))
    canvas.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    body = connected.get("/canvas/overview").json()
    assert body["stale"] is True and body["busy"] is False
    assert len(body["upcoming"]) == 3


def test_unreachable_canvas_with_nothing_cached_is_unavailable(connected, canvas):
    canvas.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    response = connected.get("/canvas/overview")
    assert (response.status_code, response.json()["detail"]) == (502, "canvas_unavailable")


def test_kansos_own_limit_serves_the_cache_or_429s(connected, canvas, monkeypatch):
    monkeypatch.setattr(canvas_data_crud, "_fetch_limiter", RateLimiter(max_calls=1, period=60))
    connected.get("/canvas/courses")
    monkeypatch.setitem(canvas_data_crud.TTL, "courses", timedelta(0))
    assert connected.get("/canvas/courses").json()["stale"] is True
    assert connected.get("/canvas/courses/11/assignments").status_code == 429
    assert len(canvas.requests) == 1


def test_replacing_or_removing_the_token_drops_the_cache(connected, canvas):
    connected.get("/canvas/courses")
    connected.put("/canvas/connection", json={"base_url": BASE_URL, "token": NEW_TOKEN})
    canvas.requests.clear()
    connected.get("/canvas/courses")
    assert canvas.paths() == ["/api/v1/courses"]
    assert canvas.requests[0].headers["Authorization"] == f"Bearer {NEW_TOKEN}"

    connected.delete("/canvas/connection")
    assert connected.get("/canvas/courses").status_code == 404


def test_hiding_a_course_applies_to_cached_data_at_once(connected, canvas):
    connected.get("/canvas/courses")
    connected.patch("/canvas/connection", json={"hidden_course_ids": [11]})
    assert [c["id"] for c in connected.get("/canvas/courses").json()["courses"]] == [12, 14]
    assert len(canvas.requests) == 1


# ── Invalid tokens ───────────────────────────────────────────────────────────

def test_a_401_marks_the_connection_invalid_and_stops_calling_canvas(connected, db, canvas, monkeypatch):
    connected.get("/canvas/courses")
    monkeypatch.setitem(canvas_data_crud.TTL, "courses", timedelta(0))
    del canvas.valid[TOKEN]
    response = connected.get("/canvas/courses")
    assert (response.status_code, response.json()["detail"]) == (409, "canvas_token_invalid")
    row = get_connection(db, USER_ID)
    assert row.status == "invalid" and row.last_error_at is not None
    assert connected.get("/canvas/connection").json()["status"] == "invalid"

    canvas.requests.clear()
    for path in ("/canvas/courses", "/canvas/overview", "/canvas/courses/11/modules"):
        assert connected.get(path).json()["detail"] == "canvas_token_invalid"
    assert canvas.requests == []


def test_an_expired_token_is_refused_without_calling_canvas(connected, db, canvas):
    get_connection(db, USER_ID).token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()
    response = connected.get("/canvas/overview")
    assert (response.status_code, response.json()["detail"]) == (409, "canvas_token_invalid")
    assert canvas.requests == []


def test_a_token_that_no_longer_decrypts_needs_reconnecting(connected, db, canvas, monkeypatch):
    monkeypatch.setenv(crypto.KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    assert connected.get("/canvas/courses").json()["detail"] == "canvas_token_invalid"
    assert get_connection(db, USER_ID).status == "invalid"
    assert canvas.requests == []


def test_a_token_replaced_during_a_fetch_is_not_marked_invalid(connected, db, canvas):
    def replace_then_reject(request):
        canvas.respond = None
        row = get_connection(db, USER_ID)
        encrypted = crypto.encrypt(NEW_TOKEN, USER_ID)
        row.token_ciphertext, row.token_nonce = encrypted.ciphertext, encrypted.nonce
        row.token_last4 = NEW_TOKEN[-4:]
        db.commit()
        canvas_cache.evict_user(USER_ID)
        del canvas.valid[TOKEN]
        return None

    canvas.respond = replace_then_reject
    response = connected.get("/canvas/courses")
    assert (response.status_code, response.json()["detail"]) == (409, "canvas_token_invalid")
    assert get_connection(db, USER_ID).status == "active"

    canvas.requests.clear()
    assert connected.get("/canvas/courses").status_code == 200
    assert canvas.requests[0].headers["Authorization"] == f"Bearer {NEW_TOKEN}"


def test_a_token_replaced_during_a_fetch_does_not_cache_the_old_tokens_data(connected, db, canvas):
    def replace(request):
        canvas.respond = None
        row = get_connection(db, USER_ID)
        encrypted = crypto.encrypt(NEW_TOKEN, USER_ID)
        row.token_ciphertext, row.token_nonce = encrypted.ciphertext, encrypted.nonce
        db.commit()
        canvas_cache.evict_user(USER_ID)
        return None

    canvas.respond = replace
    assert connected.get("/canvas/courses").status_code == 200
    assert get_connection(db, USER_ID).last_synced_at is None

    canvas.requests.clear()
    connected.get("/canvas/courses")
    assert canvas.requests[0].headers["Authorization"] == f"Bearer {NEW_TOKEN}"


def test_removing_canvas_during_a_fetch_leaves_it_removed(connected, db, canvas):
    def remove(request):
        canvas.respond = None
        delete_connection(db, USER_ID)
        canvas_cache.evict_user(USER_ID)
        return None

    canvas.respond = remove
    assert connected.get("/canvas/courses").status_code == 200
    assert get_connection(db, USER_ID) is None


def test_reconnecting_brings_the_panel_back(connected, canvas):
    del canvas.valid[TOKEN]
    connected.get("/canvas/courses")
    assert connected.put("/canvas/connection", json={"base_url": BASE_URL, "token": NEW_TOKEN}).status_code == 200
    assert connected.get("/canvas/courses").status_code == 200


def test_the_token_never_appears_in_read_responses(connected, canvas):
    for path in ("/canvas/overview", "/canvas/courses", "/canvas/courses/11/modules",
                 "/canvas/courses/11/assignments", "/canvas/announcements"):
        response = connected.get(path)
        assert response.status_code == 200
        assert TOKEN not in response.text and TOKEN[-8:] not in response.text


# ── Demo sandboxes ───────────────────────────────────────────────────────────

DEMO_PATHS = ["/canvas/overview", "/canvas/courses", "/canvas/courses/1001/modules",
              "/canvas/courses/1001/assignments", "/canvas/announcements"]


@pytest.mark.parametrize("path", DEMO_PATHS)
def test_demo_sandboxes_get_sample_data_without_calling_canvas(demo_client, db, canvas, path):
    first = demo_client.get(path, params={"tz": "America/Los_Angeles"})
    second = demo_client.get(path, params={"tz": "America/Los_Angeles"})
    assert first.status_code == 200
    assert first.json()["sample"] is True
    assert {k: v for k, v in first.json().items() if k != "fetched_at"} == {
        k: v for k, v in second.json().items() if k != "fetched_at"
    }
    assert canvas.requests == []
    assert get_connection(db, USER_ID) is None


def test_demo_sample_data_fills_the_panel(demo_client, canvas):
    overview = demo_client.get("/canvas/overview").json()
    assert len(overview["upcoming"]) >= 6
    assert any(i["kind"] == "quiz" for i in overview["upcoming"])
    assert any(i["submission"]["late"] for i in overview["upcoming"])
    assert [i["submission"]["missing"] for i in overview["missing"]] == [True]
    assert [i["id"] for i in overview["recently_graded"]] == [5200, 5100]
    assert all(i["submission"]["graded"] for i in overview["recently_graded"])
    start, end = datetime.fromisoformat(overview["start"]), datetime.fromisoformat(overview["end"])
    assert all(start <= datetime.fromisoformat(i["due_at"]) < end for i in overview["upcoming"])

    courses = demo_client.get("/canvas/courses").json()["courses"]
    assert len(courses) == 5
    assert [c["course_code"] for c in courses if c["grades_hidden"]] == ["COMM 103"]
    modules = demo_client.get("/canvas/courses/1001/modules").json()["modules"]
    assert modules[-1]["state"] == "locked"
    assert len(demo_client.get("/canvas/announcements").json()["announcements"]) == 3
    assert demo_client.get("/canvas/courses/1002/modules").json()["modules"] == []


def test_demo_sample_course_not_found(demo_client, canvas):
    response = demo_client.get("/canvas/courses/5/assignments")
    assert (response.status_code, response.json()["detail"]) == (404, "canvas_course_not_found")


def test_demo_sandboxes_still_cannot_connect(demo_client, canvas):
    assert demo_client.get("/canvas/connection").status_code == 403


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/courses/1/assignments/2", f"{BASE_URL}/courses/1/assignments/2"),
        (f"{BASE_URL}/courses/1", f"{BASE_URL}/courses/1"),
        ("https://SDSU.instructure.com/courses/1", "https://SDSU.instructure.com/courses/1"),
        ("//evil.example.com/x", None),
        ("/\\evil.example.com", None),
        ("javascript:alert(1)", None),
        ("http://sdsu.instructure.com/courses/1", None),
        ("https://sdsu.instructure.com.evil.com/courses/1", None),
        ("https://sdsu.instructure.com:8443/courses/1", None),
        ("courses/1", None),
        (None, None),
    ],
)
def test_links_are_kept_only_on_the_school_host(value, expected):
    assert canvas_url(BASE_URL, value) == expected


def test_plain_text_strips_markup_and_cuts_long_messages():
    assert plain_text("<div>Hi&amp;bye<br>next <style>p{}</style>line</div>") == "Hi&bye\nnext line"
    assert plain_text("<p>" + "x" * 50 + "</p>", limit=10) == "x" * 9 + "…"
