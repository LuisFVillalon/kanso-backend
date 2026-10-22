"""
Canvas data for the home-page panel: upcoming work, missing work, courses
with grades, a course's modules and assignments, and announcements.

Every read goes through read(), which serves the in-process cache
(core/canvas_cache.py) while it's fresh and otherwise calls Canvas with the
user's decrypted token, or fetches their calendar feed:

- A calendar feed connection has only the "feed" kind (its items and their
  dates), and a token connection everything else; any other read raises
  CanvasNotSupported without calling Canvas.

- A connection already marked invalid, or past its token's expiry date,
  raises CanvasTokenInvalid without calling Canvas.
- A 401 from Canvas, a feed Canvas no longer knows, or a secret that no
  longer decrypts marks the connection invalid, so the panel shows
  "Reconnect Canvas", and drops the user's cached data.
- The transaction ends before Canvas is called. A fetch's outcome is only
  recorded (and cached) if the connection still holds the token it used:
  one replaced or removed meanwhile is left alone.
- If Canvas is rate-limiting after the client's backoff, or is unreachable,
  the last cached data is served with busy / stale set; with nothing
  cached, the error is raised.
- refresh=True skips a fresh cache, but not one fetched under
  MIN_REFRESH_AGE ago, so a mashed Refresh button costs one Canvas call.

Calls that reach Canvas are limited per user (_fetch_limiter), and at most
MAX_IN_FLIGHT run at once per user: Canvas charges parallel requests extra.
A fetch that can't get a slot within IN_FLIGHT_WAIT counts as Canvas
being unavailable.
The cache holds every course; hidden_course_ids is applied on the way out.
"""

import html
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.core import canvas_cache, crypto
from app.core.canvas_client import (
    CanvasClient,
    CanvasError,
    CanvasRateLimited,
    CanvasTokenInvalid,
    CanvasUnavailable,
)
from app.core.canvas_feed import CanvasFeed, CanvasFeedInvalid, FeedEvent
from app.core.rate_limit import RateLimiter
from app.crud.canvas_crud import decrypt_token, expired, get_connection
from app.models.canvas_connection_model import CanvasConnection
from app.schemas.canvas_schema import (
    CanvasAnnouncement,
    CanvasAssignment,
    CanvasCourse,
    CanvasFeedItem,
    CanvasItem,
    CanvasModule,
    CanvasModuleItem,
    CanvasModuleRequirement,
    CanvasSubmission,
)

# How long each kind of data counts as fresh.
TTL = {
    "overview": timedelta(minutes=5),
    "announcements": timedelta(minutes=5),
    "courses": timedelta(minutes=15),
    "assignments": timedelta(minutes=15),
    "modules": timedelta(minutes=30),
    # The whole calendar feed, one fetch for both the overview and the linked-task sync.
    "feed": timedelta(minutes=15),
}
# The kinds a calendar feed connection can read; a token connection reads the rest.
FEED_KINDS = frozenset({"feed"})
MIN_REFRESH_AGE = timedelta(seconds=30)
MAX_IN_FLIGHT = 2
# How long a fetch waits for one of the user's MAX_IN_FLIGHT slots before serving the cache.
IN_FLIGHT_WAIT = timedelta(seconds=25)
UPCOMING_DAYS = 7
RECENTLY_GRADED_DAYS = 7
RECENTLY_GRADED_MAX = 10
ANNOUNCEMENT_DAYS = 14
ANNOUNCEMENT_MESSAGE_MAX = 2000
# Modules whose items Canvas leaves out (long ones) are fetched one by one, up to this many.
MODULE_ITEM_FETCHES_MAX = 10

_fetch_limiter = RateLimiter(max_calls=30, period=60)
_in_flight: defaultdict[str, threading.BoundedSemaphore] = defaultdict(
    lambda: threading.BoundedSemaphore(MAX_IN_FLIGHT)
)


class CanvasNotConnected(CanvasError):
    code = "canvas_not_connected"


class CanvasCourseHidden(CanvasError):
    code = "canvas_course_hidden"


class CanvasNotSupported(CanvasError):
    """The connection's type can't provide this data, e.g. courses from a calendar feed."""

    code = "canvas_not_supported"


@dataclass(frozen=True)
class Read:
    """Data from Canvas or the cache, and how fresh it is."""

    data: Any
    fetched_at: datetime
    # The connection's "Courses to show" setting, to filter data on the way out.
    hidden_course_ids: frozenset[int]
    busy: bool = False
    stale: bool = False


def read(
    db: Session,
    user_id: str,
    kind: str,
    key: tuple,
    fetch: Callable[[CanvasClient], Any] | Callable[[CanvasFeed], Any],
    now: datetime,
    refresh: bool = False,
    course_id: int | None = None,
    retry: bool = True,
) -> Read:
    """Serve kind/key for user_id from the cache, or fetch it from Canvas. See the module docstring.

    With course_id, a course unticked under "Courses to show" raises
    CanvasCourseHidden without calling Canvas. A token rejected after it was
    replaced or removed is read again once (with retry) against the current
    connection.
    """
    row = get_connection(db, user_id)
    if row is None:
        raise CanvasNotConnected("Canvas isn't connected.")
    if (row.connection_type == "feed") != (kind in FEED_KINDS):
        raise CanvasNotSupported(f"A {row.connection_type} connection can't read {kind}.")
    if row.status == "invalid" or expired(row, now):
        if row.connection_type == "feed":
            raise CanvasFeedInvalid("Canvas no longer knows the feed.")
        raise CanvasTokenInvalid("The token was rejected or has expired.")

    hidden = frozenset(row.hidden_course_ids or [])
    if course_id is not None and course_id in hidden:
        raise CanvasCourseHidden("The course is hidden under Courses to show.")
    cache_key = (user_id, row.base_url, row.canvas_user_id, kind, *key)
    cached = canvas_cache.get(cache_key)
    if cached is not None:
        age = now - cached.fetched_at
        if age < TTL[kind] and (not refresh or age < MIN_REFRESH_AGE):
            return Read(cached.data, cached.fetched_at, hidden)

    try:
        _fetch_limiter.check(user_id)
    except HTTPException:
        if cached is None:
            raise
        return Read(cached.data, cached.fetched_at, hidden, stale=True)

    connection_type, base_url, nonce = row.connection_type, row.base_url, row.token_nonce
    try:
        token = decrypt_token(row)
    except crypto.DecryptionError:
        token = None
    db.commit()

    in_flight = _in_flight[user_id]
    try:
        if token is None:
            raise CanvasTokenInvalid("The stored token can't be decrypted.")
        if not in_flight.acquire(timeout=IN_FLIGHT_WAIT.total_seconds()):
            raise CanvasUnavailable("Too many Canvas requests are already running for this user.")
        try:
            data = fetch(CanvasFeed(token) if connection_type == "feed" else CanvasClient(base_url, token))
        finally:
            in_flight.release()
    except CanvasTokenInvalid:
        if _record(db, user_id, nonce, {"status": "invalid", "last_error_at": now}):
            canvas_cache.evict_user(user_id)
            raise
        if not retry:
            raise
        return read(db, user_id, kind, key, fetch, now, refresh, course_id, retry=False)
    except CanvasUnavailable as exc:
        if cached is None:
            raise
        return Read(cached.data, cached.fetched_at, hidden, busy=isinstance(exc, CanvasRateLimited), stale=True)

    recorded = _record(db, user_id, nonce, {
        "last_synced_at": now,
        "status": case((CanvasConnection.status == "error", "active"), else_=CanvasConnection.status),
    })
    if not recorded:
        return Read(data, now, hidden)
    entry = canvas_cache.put(cache_key, data, now)
    return Read(entry.data, entry.fetched_at, hidden)


def _record(db: Session, user_id: str, nonce: bytes, values: dict) -> bool:
    """Write a fetch's outcome to the connection, unless it was removed or its token replaced meanwhile."""
    matched = (
        db.query(CanvasConnection)
        .filter(CanvasConnection.user_id == user_id, CanvasConnection.token_nonce == nonce)
        .update(values, synchronize_session=False)
    )
    db.commit()
    return matched > 0


# ── Fetchers: one per route, each returning normalized shapes ────────────────

def upcoming_window(today: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The user's local day today through UPCOMING_DAYS days later, as aware datetimes."""
    start = datetime.combine(today, time(), tzinfo=tz)
    return start, datetime.combine(today + timedelta(days=UPCOMING_DAYS), time(), tzinfo=tz)


def fetch_overview(start: datetime, end: datetime) -> Callable[[CanvasClient], dict]:
    def fetch(client: CanvasClient) -> dict:
        planner = client.get_list(
            "/planner/items",
            {
                "start_date": (start - timedelta(days=RECENTLY_GRADED_DAYS)).isoformat(),
                "end_date": end.isoformat(),
                "per_page": 100,
            },
        )
        missing = client.get_list(
            "/users/self/missing_submissions",
            {"include[]": "course", "filter[]": ["submittable", "current_grading_period"], "per_page": 100},
        )
        items = [item for raw in planner if (item := _planner_item(client.base_url, raw)) is not None]
        upcoming = [item for item in items if item.due_at is None or start <= item.due_at < end]
        upcoming.sort(key=lambda item: (item.due_at is None, item.due_at or end, item.id))
        missing_items = [item for raw in missing if (item := _missing_item(client.base_url, raw)) is not None]
        missing_items.sort(key=lambda item: (item.due_at is None, item.due_at or end, item.id))
        return {"upcoming": upcoming, "missing": missing_items, "recently_graded": recently_graded(items, start)}

    return fetch


@dataclass(frozen=True)
class FeedEntry:
    """A calendar feed item as cached, before the caller's time zone is known."""

    # due_at is None for an all-day item.
    item: CanvasFeedItem
    # The day an all-day item falls on.
    day: date | None = None


def fetch_feed(feed: CanvasFeed) -> list[FeedEntry]:
    return [_feed_entry(feed.base_url, event) for event in feed.events()]


def feed_zone(connection: CanvasConnection) -> ZoneInfo:
    """The zone a feed's all-day items are due in: the one stored when it was connected, else UTC."""
    return ZoneInfo(connection.time_zone or "UTC")


def feed_items(entries: list[FeedEntry], tz: ZoneInfo) -> list[CanvasFeedItem]:
    """The feed's items with every due_at set: an all-day assignment is due at 11:59:59 pm in tz, anything else at 0:00.

    tz is the connection's feed_zone, so the overview and the sync give the same due_at whatever the caller's zone.
    """
    items = []
    for entry in entries:
        if entry.day is None:
            items.append(entry.item)
            continue
        at = time(23, 59, 59) if entry.item.kind == "assignment" else time()
        items.append(entry.item.model_copy(update={"due_at": datetime.combine(entry.day, at, tzinfo=tz)}))
    return items


def feed_upcoming(entries: list[FeedEntry], tz: ZoneInfo, start: datetime, end: datetime) -> list[CanvasFeedItem]:
    """The feed's items due in [start, end), soonest first, like the overview's upcoming list. tz as for feed_items."""
    upcoming = [item for item in feed_items(entries, tz) if start <= item.due_at < end]
    upcoming.sort(key=lambda item: (item.due_at, item.id))
    return upcoming


def recently_graded(items: list[CanvasItem], start: datetime) -> list[CanvasItem]:
    """Graded items due in the RECENTLY_GRADED_DAYS before start, newest first, at most RECENTLY_GRADED_MAX."""
    since = start - timedelta(days=RECENTLY_GRADED_DAYS)
    graded = [
        item for item in items
        if item.submission is not None and item.submission.graded
        and item.due_at is not None and since <= item.due_at < start
    ]
    graded.sort(key=lambda item: (item.due_at, item.id), reverse=True)
    return graded[:RECENTLY_GRADED_MAX]


def fetch_courses(client: CanvasClient) -> list[CanvasCourse]:
    courses = client.get_list(
        "/courses",
        {
            "enrollment_state": "active",
            "include[]": ["total_scores", "current_grading_period_scores", "term"],
            "per_page": 100,
        },
    )
    return [course for raw in courses if (course := _course(client.base_url, raw)) is not None]


def fetch_modules(course_id: int) -> Callable[[CanvasClient], list[CanvasModule]]:
    def fetch(client: CanvasClient) -> list[CanvasModule]:
        raw_modules = client.get_list(
            f"/courses/{course_id}/modules", {"include[]": ["items", "content_details"], "per_page": 50}
        )
        modules = []
        lookups = 0
        for raw in raw_modules:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
                continue
            raw_items = raw.get("items")
            complete = True
            if not isinstance(raw_items, list):
                # Canvas leaves the items out of long modules.
                raw_items = []
                if _int(raw.get("items_count")):
                    if lookups < MODULE_ITEM_FETCHES_MAX:
                        lookups += 1
                        raw_items = client.get_list(
                            f"/courses/{course_id}/modules/{raw['id']}/items",
                            {"include[]": "content_details", "per_page": 100},
                        )
                    else:
                        complete = False
            modules.append(_module(client.base_url, raw, raw_items, complete))
        modules.sort(key=lambda module: (module.position is None, module.position or 0, module.id))
        return modules

    return fetch


def fetch_assignments(course_id: int) -> Callable[[CanvasClient], list[CanvasAssignment]]:
    def fetch(client: CanvasClient) -> list[CanvasAssignment]:
        raw = client.get_list(
            f"/courses/{course_id}/assignments",
            {"include[]": "submission", "order_by": "due_at", "per_page": 100},
        )
        return [a for item in raw if (a := _assignment(client.base_url, item, course_id)) is not None]

    return fetch


def fetch_announcements(course_ids: list[int], today: date) -> Callable[[CanvasClient], list[CanvasAnnouncement]]:
    def fetch(client: CanvasClient) -> list[CanvasAnnouncement]:
        if not course_ids:
            return []
        raw = client.get_list(
            "/announcements",
            {
                "context_codes[]": [f"course_{course_id}" for course_id in course_ids],
                "start_date": (today - timedelta(days=ANNOUNCEMENT_DAYS)).isoformat(),
                "end_date": (today + timedelta(days=1)).isoformat(),
                "per_page": 50,
            },
        )
        announcements = [a for item in raw if (a := _announcement(client.base_url, item)) is not None]
        announcements.sort(key=lambda a: (a.posted_at is None, -(a.posted_at.timestamp() if a.posted_at else 0)))
        return announcements

    return fetch


# ── Normalizing Canvas JSON ──────────────────────────────────────────────────

def _feed_entry(base_url: str, event: FeedEvent) -> FeedEntry:
    # Point an assignment at its own page, as the API's html_url does, rather
    # than at the calendar the feed links to.
    if event.kind == "assignment" and event.course_id is not None:
        html_url = f"{base_url}/courses/{event.course_id}/assignments/{event.id}"
    else:
        html_url = canvas_url(base_url, event.url)
    all_day = not isinstance(event.start, datetime)
    item = CanvasFeedItem(
        id=event.id,
        kind=event.kind,
        title=event.title,
        course_id=event.course_id,
        course_name=event.course_name,
        due_at=None if all_day else event.start,
        html_url=html_url,
        uid=event.uid,
        all_day=all_day,
        linkable=event.kind == "assignment",
    )
    return FeedEntry(item=item, day=event.start if all_day else None)


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def canvas_url(base_url: str, value) -> str | None:
    """value as an absolute https link on the school's Canvas host, or None for anything else.

    The frontend renders these as links, so a javascript: URL or a link to
    another site from a hostile Canvas host never gets through.
    """
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return f"{base_url}{value}"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return None
    if f"https://{(parts.hostname or '').lower()}" != base_url:
        return None
    return value


def _planner_submission(raw) -> CanvasSubmission | None:
    if not isinstance(raw, dict):
        return None
    flags = ("submitted", "missing", "late", "graded", "excused")
    return CanvasSubmission(**{flag: raw.get(flag) is True for flag in flags})


_PLANNER_KINDS = {
    "assignment": "assignment",
    "quiz": "quiz",
    "discussion_topic": "discussion",
    "wiki_page": "page",
    "calendar_event": "event",
    "planner_note": "note",
}


def _planner_item(base_url: str, raw) -> CanvasItem | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("plannable"), dict):
        return None
    plannable = raw["plannable"]
    item_id = _int(raw.get("plannable_id"))
    title = _str(plannable.get("title")) or _str(plannable.get("name"))
    if item_id is None or title is None:
        return None
    kind = _PLANNER_KINDS.get(raw.get("plannable_type"), "other")
    if kind == "assignment" and plannable.get("is_quiz_assignment") is True:
        kind = "quiz"
    return CanvasItem(
        id=item_id,
        kind=kind,
        title=title,
        course_id=_int(raw.get("course_id")),
        course_name=_str(raw.get("context_name")),
        due_at=_dt(plannable.get("due_at")) or _dt(raw.get("plannable_date")),
        points_possible=_float(plannable.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=_planner_submission(raw.get("submissions")),
    )


def _assignment_kind(raw: dict) -> str:
    types = raw.get("submission_types")
    if raw.get("is_quiz_assignment") is True or (isinstance(types, list) and "online_quiz" in types):
        return "quiz"
    if isinstance(types, list) and "discussion_topic" in types:
        return "discussion"
    return "assignment"


def _missing_item(base_url: str, raw) -> CanvasItem | None:
    if not isinstance(raw, dict) or _int(raw.get("id")) is None or _str(raw.get("name")) is None:
        return None
    course = raw.get("course") if isinstance(raw.get("course"), dict) else {}
    return CanvasItem(
        id=_int(raw["id"]),
        kind=_assignment_kind(raw),
        title=raw["name"],
        course_id=_int(raw.get("course_id")),
        course_name=_str(course.get("course_code")) or _str(course.get("name")),
        due_at=_dt(raw.get("due_at")),
        points_possible=_float(raw.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=CanvasSubmission(missing=True),
    )


def _submission(raw) -> CanvasSubmission | None:
    if not isinstance(raw, dict):
        return None
    grade = raw.get("grade")
    return CanvasSubmission(
        submitted=_dt(raw.get("submitted_at")) is not None,
        missing=raw.get("missing") is True,
        late=raw.get("late") is True,
        graded=raw.get("workflow_state") == "graded",
        excused=raw.get("excused") is True,
        score=_float(raw.get("score")),
        grade=grade if isinstance(grade, str) else None,
        submitted_at=_dt(raw.get("submitted_at")),
        graded_at=_dt(raw.get("graded_at")),
    )


def _assignment(base_url: str, raw, course_id: int) -> CanvasAssignment | None:
    if not isinstance(raw, dict) or _int(raw.get("id")) is None or _str(raw.get("name")) is None:
        return None
    types = raw.get("submission_types")
    return CanvasAssignment(
        id=_int(raw["id"]),
        kind=_assignment_kind(raw),
        title=raw["name"],
        course_id=course_id,
        due_at=_dt(raw.get("due_at")),
        points_possible=_float(raw.get("points_possible")),
        html_url=canvas_url(base_url, raw.get("html_url")),
        submission=_submission(raw.get("submission")),
        unlock_at=_dt(raw.get("unlock_at")),
        lock_at=_dt(raw.get("lock_at")),
        quiz_id=_int(raw.get("quiz_id")),
        submission_types=[t for t in types if isinstance(t, str)] if isinstance(types, list) else [],
    )


def _course(base_url: str, raw) -> CanvasCourse | None:
    # Courses outside their dates come back as just {"id", "access_restricted_by_date"}.
    if not isinstance(raw, dict) or _int(raw.get("id")) is None or _str(raw.get("name")) is None:
        return None
    course_id = _int(raw["id"])
    enrollments = raw.get("enrollments") if isinstance(raw.get("enrollments"), list) else []
    student = next(
        (e for e in enrollments if isinstance(e, dict) and e.get("type") in ("student", "StudentEnrollment")), {}
    )
    term = raw.get("term") if isinstance(raw.get("term"), dict) else {}
    hidden_grades = raw.get("hide_final_grades") is True

    def grade(field):
        return None if hidden_grades else student.get(field)

    return CanvasCourse(
        id=course_id,
        name=raw["name"],
        course_code=_str(raw.get("course_code")),
        term=_str(term.get("name")),
        html_url=f"{base_url}/courses/{course_id}",
        grades_hidden=hidden_grades,
        current_score=_float(grade("computed_current_score")),
        current_grade=_str(grade("computed_current_grade")),
        grading_period=_str(student.get("current_grading_period_title")),
        period_score=_float(grade("current_period_computed_current_score")),
        period_grade=_str(grade("current_period_computed_current_grade")),
    )


def _module_item(base_url: str, raw) -> CanvasModuleItem | None:
    if not isinstance(raw, dict) or _int(raw.get("id")) is None or _str(raw.get("title")) is None:
        return None
    details = raw.get("content_details") if isinstance(raw.get("content_details"), dict) else {}
    requirement = raw.get("completion_requirement")
    return CanvasModuleItem(
        id=_int(raw["id"]),
        title=raw["title"],
        type=_str(raw.get("type")) or "Other",
        html_url=canvas_url(base_url, raw.get("html_url")),
        indent=_int(raw.get("indent")) or 0,
        locked=details.get("locked_for_user") is True,
        due_at=_dt(details.get("due_at")),
        points_possible=_float(details.get("points_possible")),
        requirement=CanvasModuleRequirement(
            type=requirement["type"],
            min_score=_float(requirement.get("min_score")),
            completed=requirement.get("completed") is True,
        )
        if isinstance(requirement, dict) and _str(requirement.get("type"))
        else None,
    )


def _module(base_url: str, raw: dict, raw_items: list, complete: bool) -> CanvasModule:
    return CanvasModule(
        id=raw["id"],
        name=_str(raw.get("name")) or "Untitled module",
        position=_int(raw.get("position")),
        state=_str(raw.get("state")),
        unlock_at=_dt(raw.get("unlock_at")),
        completed_at=_dt(raw.get("completed_at")),
        require_sequential_progress=raw.get("require_sequential_progress") is True,
        items=[item for r in raw_items if (item := _module_item(base_url, r)) is not None],
        items_complete=complete,
    )


class _Text(HTMLParser):
    """Collects an HTML fragment's text, leaving out scripts and styles."""

    _SKIP = {"script", "style"}
    _BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def plain_text(fragment: str, limit: int = ANNOUNCEMENT_MESSAGE_MAX) -> str:
    """An HTML fragment as plain text: tags dropped, whitespace tidied, cut to limit characters."""
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _announcement(base_url: str, raw) -> CanvasAnnouncement | None:
    if not isinstance(raw, dict) or _int(raw.get("id")) is None or _str(raw.get("title")) is None:
        return None
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    context = raw.get("context_code")
    course_id = context.removeprefix("course_") if isinstance(context, str) and context.startswith("course_") else None
    read_state = raw.get("read_state")
    return CanvasAnnouncement(
        id=_int(raw["id"]),
        title=raw["title"],
        message=plain_text(raw["message"]) if isinstance(raw.get("message"), str) else "",
        posted_at=_dt(raw.get("posted_at")),
        author=_str(author.get("display_name")),
        read={"read": True, "unread": False}.get(read_state),
        course_id=_int(course_id),
        html_url=canvas_url(base_url, raw.get("html_url")),
    )
