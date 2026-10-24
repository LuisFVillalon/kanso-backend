"""
Canvas LMS endpoints: the connection, and the data shown in the home-page panel.

GET    /canvas/connection       — the masked connection status and its capabilities, never the token or feed URL
PUT    /canvas/connection       — connect, replace or reconnect: checks the token with Canvas, then stores it encrypted
PUT    /canvas/connection/feed  — the same with a calendar feed link: fetches and parses it, then stores it encrypted
POST   /canvas/connection/test  — re-checks the stored token with Canvas, or fetches the feed
PATCH  /canvas/connection       — changes settings (the courses to hide, and auto_complete)
DELETE /canvas/connection       — hard-deletes the connection and its token

GET    /canvas/overview                        — due this week (or all upcoming), missing and recently graded work
GET    /canvas/classes                         — the user's classes, with what's due in each
GET    /canvas/courses                         — active courses with current grades
GET    /canvas/courses/{course_id}/modules     — a course's modules and their items
GET    /canvas/courses/{course_id}/assignments — a course's assignments and quizzes, with the user's submissions
GET    /canvas/announcements                   — the last 14 days of announcements
POST   /canvas/linked-tasks/sync               — tasks added from Canvas: completed once done, due dates moved

GET    /canvas/course-tags                     — each class's default tags for tasks added from it
PUT    /canvas/course-tags/{course_id}         — sets (or clears) a class's default tags

A calendar feed connection (core/canvas_feed.py) has dates only: the
overview's upcoming list (missing and recently_graded stay empty), the
classes its items belong to, and the linked-task sync's due dates. Courses, modules, assignments and
announcements answer 404 canvas_not_supported for it; GET /canvas/connection
says which type is connected and what it can show.

The read routes proxy Canvas through a short cache (crud/canvas_data_crud.py)
and return fetched_at, busy and stale with the data. A course's assignments
and the overview's missing work are marked with what the user checked off in
Canvas's planner (its planner overrides); overrides Canvas can't serve, or that
run past the page cap, leave that out, with stale set. Demo sandboxes get
read-only sample data from them (crud/canvas_sample.py), with sample=true and
no Canvas call, so the panel can be shown off. The linked-task sync
(crud/canvas_link_crud.py) reads assignments the same way, and syncs demo
sandboxes against the sample data.

Demo sandboxes are refused with 403 on the connection routes: they'd be
handing a real Canvas token to a throwaway account. Canvas-specific failures return a stable code as
the detail (see core/canvas_client.py), so the frontend can show the right
message: canvas_url_invalid, canvas_not_canvas, canvas_token_invalid,
canvas_forbidden (422), canvas_account_changed (409), canvas_not_connected (404),
canvas_feed_url_invalid and canvas_feed_invalid (422: a feed link that isn't
one, or one Canvas doesn't know), canvas_unavailable (502) and
canvas_not_configured (503, the server has no CANVAS_TOKEN_KEY). The read routes answer canvas_token_invalid with 409
("Reconnect Canvas"), and add canvas_busy (502: Canvas is rate-limiting and
nothing is cached), canvas_course_hidden and canvas_course_not_found (404).
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core import canvas_cache, crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import (
    CanvasError,
    CanvasNotFound,
    CanvasRateLimited,
    CanvasTokenInvalid,
    CanvasUnavailable,
)
from app.core.http_utils import require_time_zone
from app.core.rate_limit import RateLimiter
from app.crud import canvas_data_crud, canvas_link_crud, canvas_sample
from app.crud.canvas_crud import (
    CanvasAccountChanged,
    check_connection,
    connect,
    connect_feed,
    connection_out,
    course_tags,
    delete_connection,
    get_connection,
    set_course_tags,
    update_connection,
)
from app.crud.canvas_data_crud import CanvasCourseHidden, CanvasNotConnected, CanvasNotSupported
from app.database.database import get_db
from app.schemas.canvas_schema import (
    CanvasAnnouncementsOut,
    CanvasAssignmentsOut,
    CanvasClassesOut,
    CanvasConnect,
    CanvasConnectionOut,
    CanvasConnectionUpdate,
    CanvasCoursesOut,
    CanvasCourseTagsOut,
    CanvasCourseTagsUpdate,
    CanvasFeedConnect,
    CanvasLinkedSyncOut,
    CanvasModulesOut,
    CanvasOverviewOut,
    CanvasRange,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/canvas", tags=["canvas"])

# Connect and test each call Canvas with the user's token; this keeps a stuck
# client from burning the user's Canvas quota or probing addresses.
_canvas_call_limiter = RateLimiter(max_calls=10, period=60)


def _real_user(current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
    if current_user.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Canvas can't be connected to a demo sandbox. Sign up for your own account to connect it.",
        )
    return current_user


def _canvas_http_error(exc: CanvasError | crypto.EncryptionKeyError) -> HTTPException:
    if isinstance(exc, crypto.EncryptionKeyError):
        log.error("CANVAS | %s", exc)
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="canvas_not_configured")
    if isinstance(exc, CanvasUnavailable):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.code)
    if isinstance(exc, CanvasAccountChanged):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.code)


def _not_connected() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_not_connected")


@router.get("/connection", response_model=CanvasConnectionOut)
def read_connection(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Return the connection's status: host, the token's last 4 characters, account, expiry. connected=false if none."""
    return connection_out(get_connection(db, current_user.id), datetime.now(UTC))


@router.put("/connection", response_model=CanvasConnectionOut)
def connect_canvas(
    body: CanvasConnect,
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Check the token with the school's Canvas, then store it encrypted. On failure nothing changes."""
    _canvas_call_limiter.check(current_user.id)
    try:
        row = connect(db, current_user.id, body.base_url, body.token.get_secret_value(), body.allow_account_change)
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _canvas_http_error(exc) from None
    canvas_cache.evict_user(current_user.id)
    return connection_out(row, datetime.now(UTC))


class _FeedLinkRoute(APIRoute):
    """Answers a malformed body with a generic 422 canvas_feed_url_invalid.

    FastAPI's validation errors echo the submitted value, which here may be
    the feed link (e.g. sent as a non-string, or as the whole body).
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="canvas_feed_url_invalid"
                ) from None

        return route_handler


def connect_canvas_feed(
    body: CanvasFeedConnect,
    tz: str = Query("UTC", description="The IANA time zone the feed's all-day items are due in."),
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Fetch and parse the calendar feed, then store its link encrypted. On failure nothing changes."""
    zone = require_time_zone(tz)
    _canvas_call_limiter.check(current_user.id)
    try:
        row = connect_feed(
            db, current_user.id, body.feed_url.get_secret_value(), zone.key, body.allow_account_change
        )
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _canvas_http_error(exc) from None
    canvas_cache.evict_user(current_user.id)
    return connection_out(row, datetime.now(UTC))


router.add_api_route(
    "/connection/feed", connect_canvas_feed, methods=["PUT"], response_model=CanvasConnectionOut,
    route_class_override=_FeedLinkRoute,
)


@router.post("/connection/test", response_model=CanvasConnectionOut)
def test_canvas_connection(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Re-check the stored token with Canvas. A rejected or expired token comes back as status "invalid"."""
    _canvas_call_limiter.check(current_user.id)
    now = datetime.now(UTC)
    try:
        row = check_connection(db, current_user.id, now)
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _canvas_http_error(exc) from None
    if row is None:
        raise _not_connected()
    return connection_out(row, now)


@router.patch("/connection", response_model=CanvasConnectionOut)
def update_canvas_connection(
    changes: CanvasConnectionUpdate,
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Set the Canvas course ids hidden from the panel, and whether the linked-task sync completes tasks."""
    row = update_connection(db, current_user.id, changes.hidden_course_ids, changes.auto_complete)
    if row is None:
        raise _not_connected()
    return connection_out(row, datetime.now(UTC))


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
def remove_canvas_connection(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the connection and its token. There is no trash for it."""
    delete_connection(db, current_user.id)
    canvas_cache.evict_user(current_user.id)


# ── Panel data ───────────────────────────────────────────────────────────────

_TZ = Query("UTC", description="The caller's IANA time zone, which decides what 'today' is.")
_REFRESH = Query(False, description="Fetch from Canvas even if the cached data is fresh (at most every 30 seconds).")
_COURSE_ID = Path(ge=1)
_INCLUDE_HIDDEN = Query(False, description="Also list courses unticked under Courses to show, for Settings.")


def _read_http_error(exc: CanvasError | crypto.EncryptionKeyError) -> HTTPException:
    if isinstance(exc, CanvasNotConnected):
        return _not_connected()
    if isinstance(exc, CanvasTokenInvalid):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    if isinstance(exc, CanvasRateLimited):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="canvas_busy", headers={"Retry-After": "30"}
        )
    if isinstance(exc, CanvasCourseHidden | CanvasNotSupported):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.code)
    return _canvas_http_error(exc)


def _read(db: Session, current_user: UserInfo, *args, **kwargs) -> canvas_data_crud.Read:
    try:
        return canvas_data_crud.read(db, current_user.id, *args, now=datetime.now(UTC), **kwargs)
    except CanvasNotFound as exc:
        if kwargs.get("course_id") is None:
            raise _read_http_error(exc) from None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_course_not_found") from None
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _read_http_error(exc) from None


def _freshness(result: canvas_data_crud.Read) -> dict:
    return {"fetched_at": result.fetched_at, "busy": result.busy, "stale": result.stale}


def _marked_complete(
    db: Session, current_user: UserInfo, result: canvas_data_crud.Read, items: list, refresh: bool
) -> tuple[list, dict]:
    """items with marked_complete from the planner overrides, and the freshness of result and the overrides together."""
    if not items:
        return items, _freshness(result)
    try:
        overrides = canvas_data_crud.read_marked_complete(db, current_user.id, datetime.now(UTC), refresh)
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _read_http_error(exc) from None
    return canvas_data_crud.with_marked_complete(items, overrides.data), {
        "fetched_at": min(result.fetched_at, overrides.fetched_at),
        "busy": result.busy or overrides.busy,
        "stale": result.stale or overrides.stale,
    }


def _sample() -> dict:
    return {"fetched_at": datetime.now(UTC), "sample": True}


def _no_sample_course() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_course_not_found")


@router.get("/overview", response_model=CanvasOverviewOut)
def read_canvas_overview(
    tz: str = _TZ,
    range_: CanvasRange = Query(
        "week", alias="range", description="week: the next 7 days. all: everything upcoming, about 13 months.",
    ),
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Work due from the start of today through the range's end, soonest first, missing and recently graded work.

    A calendar feed gives the upcoming work only.
    """
    zone = require_time_zone(tz)
    today = datetime.now(zone).date()
    start, end = canvas_data_crud.upcoming_window(today, zone, range_)
    window = {"range": range_, "start": start, "end": end}
    if current_user.is_anonymous:
        return CanvasOverviewOut(**_sample(), **window, **canvas_sample.overview(today, zone, start, end))
    connection = get_connection(db, current_user.id)
    if connection is not None and connection.connection_type == "feed":
        result = _read(db, current_user, "feed", (), canvas_data_crud.fetch_feed, refresh=refresh)
        upcoming = canvas_data_crud.feed_upcoming(result.data, canvas_data_crud.feed_zone(connection), start, end)
        return CanvasOverviewOut(
            **_freshness(result), **window, missing=[], recently_graded=[],
            upcoming=[item for item in upcoming if item.course_id not in result.hidden_course_ids],
        )
    result = _read(
        db, current_user, "overview", (zone.key, today.isoformat(), range_),
        canvas_data_crud.fetch_overview(start, end), refresh=refresh,
    )
    shown = {key: [item for item in items if item.course_id not in result.hidden_course_ids]
             for key, items in result.data.items()}
    shown["missing"], freshness = _marked_complete(db, current_user, result, shown["missing"], refresh)
    return CanvasOverviewOut(**freshness, **window, **shown)


@router.get("/classes", response_model=CanvasClassesOut)
def read_canvas_classes(
    tz: str = _TZ,
    include_hidden: bool = _INCLUDE_HIDDEN,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's classes by code, each with how much is due this week and in all, and the next item due.

    A calendar feed's classes are the courses its items belong to, from the
    cached feed, with no name or term; a token's are its active courses, with
    counts from the overview's "all" range.
    """
    zone = require_time_zone(tz)
    now = datetime.now(UTC)
    today = now.astimezone(zone).date()
    start, end = canvas_data_crud.upcoming_window(today, zone, "all")
    if current_user.is_anonymous:
        found = canvas_data_crud.course_classes(canvas_sample.courses())
        items = canvas_sample.overview(today, zone, start, end)["upcoming"]
        return CanvasClassesOut(**_sample(), classes=canvas_data_crud.classes(found, items, today, zone, now))
    connection = get_connection(db, current_user.id)
    if connection is not None and connection.connection_type == "feed":
        result = _read(db, current_user, "feed", (), canvas_data_crud.fetch_feed, refresh=refresh)
        found = canvas_data_crud.feed_classes(result.data, connection.base_url)
        items = canvas_data_crud.feed_upcoming(result.data, canvas_data_crud.feed_zone(connection), start, end)
        freshness = _freshness(result)
    else:
        courses = _read(db, current_user, "courses", (), canvas_data_crud.fetch_courses, refresh=refresh)
        result = _read(
            db, current_user, "overview", (zone.key, today.isoformat(), "all"),
            canvas_data_crud.fetch_overview(start, end), refresh=refresh,
        )
        found = canvas_data_crud.course_classes(courses.data)
        items = result.data["upcoming"]
        freshness = {
            "fetched_at": min(courses.fetched_at, result.fetched_at),
            "busy": courses.busy or result.busy,
            "stale": courses.stale or result.stale,
        }
    classes = [
        found_class.model_copy(update={"hidden": found_class.id in result.hidden_course_ids})
        for found_class in canvas_data_crud.classes(found, items, today, zone, now)
    ]
    if not include_hidden:
        classes = [found_class for found_class in classes if not found_class.hidden]
    return CanvasClassesOut(**freshness, classes=classes)


@router.get("/courses", response_model=CanvasCoursesOut)
def read_canvas_courses(
    include_hidden: bool = _INCLUDE_HIDDEN,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active courses with the current score and grade, or grades_hidden when the instructor hides them."""
    if current_user.is_anonymous:
        return CanvasCoursesOut(**_sample(), courses=canvas_sample.courses())
    result = _read(db, current_user, "courses", (), canvas_data_crud.fetch_courses, refresh=refresh)
    courses = [
        course.model_copy(update={"hidden": course.id in result.hidden_course_ids}) for course in result.data
    ]
    if not include_hidden:
        courses = [course for course in courses if not course.hidden]
    return CanvasCoursesOut(**_freshness(result), courses=courses)


@router.get("/courses/{course_id}/modules", response_model=CanvasModulesOut)
def read_canvas_modules(
    course_id: int = _COURSE_ID,
    tz: str = _TZ,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The course's modules in order: state, unlock date and each item's completion requirement and lock."""
    zone = require_time_zone(tz)
    if current_user.is_anonymous:
        modules = canvas_sample.modules(course_id, datetime.now(zone).date(), zone)
        if modules is None:
            raise _no_sample_course()
        return CanvasModulesOut(**_sample(), course_id=course_id, modules=modules)
    result = _read(
        db, current_user, "modules", (course_id,), canvas_data_crud.fetch_modules(course_id),
        refresh=refresh, course_id=course_id,
    )
    return CanvasModulesOut(**_freshness(result), course_id=course_id, modules=result.data)


@router.get("/courses/{course_id}/assignments", response_model=CanvasAssignmentsOut)
def read_canvas_assignments(
    course_id: int = _COURSE_ID,
    tz: str = _TZ,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The course's assignments and quizzes by due date, each with the user's submission and grade."""
    zone = require_time_zone(tz)
    if current_user.is_anonymous:
        assignments = canvas_sample.assignments(course_id, datetime.now(zone).date(), zone)
        if assignments is None:
            raise _no_sample_course()
        return CanvasAssignmentsOut(**_sample(), course_id=course_id, assignments=assignments)
    result = _read(
        db, current_user, "assignments", (course_id,), canvas_data_crud.fetch_assignments(course_id),
        refresh=refresh, course_id=course_id,
    )
    assignments, freshness = _marked_complete(db, current_user, result, result.data, refresh)
    return CanvasAssignmentsOut(**freshness, course_id=course_id, assignments=assignments)


@router.get("/announcements", response_model=CanvasAnnouncementsOut)
def read_canvas_announcements(
    tz: str = _TZ,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Announcements from the shown courses over the last 14 days, newest first, as plain text."""
    zone = require_time_zone(tz)
    today = datetime.now(zone).date()
    if current_user.is_anonymous:
        return CanvasAnnouncementsOut(**_sample(), announcements=canvas_sample.announcements(today, zone))
    courses = _read(db, current_user, "courses", (), canvas_data_crud.fetch_courses)
    course_ids = sorted(course.id for course in courses.data)
    result = _read(
        db, current_user, "announcements", (today.isoformat(), tuple(course_ids)),
        canvas_data_crud.fetch_announcements(course_ids, today), refresh=refresh,
    )
    return CanvasAnnouncementsOut(
        fetched_at=result.fetched_at,
        busy=courses.busy or result.busy,
        stale=courses.stale or result.stale,
        announcements=[a for a in result.data if a.course_id not in result.hidden_course_ids],
    )


@router.post("/linked-tasks/sync", response_model=CanvasLinkedSyncOut)
def sync_canvas_linked_tasks(
    tz: str = _TZ,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """For each open task added from Canvas: whether it's done, and its due date moved to Canvas's if changed.

    A task whose assignment is done is completed here, unless the connection's auto_complete is off.
    """
    zone = require_time_zone(tz)
    now = datetime.now(UTC)
    if current_user.is_anonymous:
        results = canvas_link_crud.sync_sample(db, current_user.id, zone, now)
        return CanvasLinkedSyncOut(**_sample(), results=results)
    try:
        result = canvas_link_crud.sync(db, current_user.id, zone, now, refresh=refresh)
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _read_http_error(exc) from None
    return CanvasLinkedSyncOut(**result)


# ── Default tags per class ───────────────────────────────────────────────────

@router.get("/course-tags", response_model=CanvasCourseTagsOut)
def read_canvas_course_tags(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Each class's default tags for tasks added from it, by course id. Empty when Canvas isn't connected."""
    return CanvasCourseTagsOut(courses=course_tags(db, current_user.id))


@router.put("/course-tags/{course_id}", response_model=CanvasCourseTagsOut)
def update_canvas_course_tags(
    body: CanvasCourseTagsUpdate,
    course_id: int = _COURSE_ID,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the class's default tags to the user's tags tag_ids, in order ([] clears them), and return every class's."""
    if set_course_tags(db, current_user.id, course_id, body.tag_ids) is None:
        raise _not_connected()
    return CanvasCourseTagsOut(courses=course_tags(db, current_user.id))
