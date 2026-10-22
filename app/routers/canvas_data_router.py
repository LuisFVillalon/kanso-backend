"""
Canvas LMS data endpoints, shown in the home-page panel.

GET    /canvas/overview                        — due in the next 7 days, missing work and recently graded work
GET    /canvas/courses                         — active courses with current grades
GET    /canvas/courses/{course_id}/modules     — a course's modules and their items
GET    /canvas/courses/{course_id}/assignments — a course's assignments and quizzes, with the user's submissions
GET    /canvas/announcements                   — the last 14 days of announcements
POST   /canvas/linked-tasks/sync               — tasks added from Canvas: submitted yet, and due date changes

A calendar feed connection (core/canvas_feed.py) has dates only: the
overview's upcoming list (missing and recently_graded stay empty) and the
linked-task sync's due dates. Courses, modules, assignments and
announcements answer 404 canvas_not_supported for it; GET /canvas/connection
says which type is connected and what it can show.

The read routes proxy Canvas through a short cache (crud/canvas_data_crud.py)
and return fetched_at, busy and stale with the data. Demo sandboxes get
read-only sample data from them (crud/canvas_sample.py), with sample=true and
no Canvas call, so the panel can be shown off. The linked-task sync
(crud/canvas_link_crud.py) reads assignments the same way, and syncs demo
sandboxes against the sample data.

Failures return the connection routes' codes (see canvas_errors.py), except
that canvas_token_invalid is a 409 ("Reconnect Canvas"), plus canvas_busy
(502: Canvas is rate-limiting and nothing is cached), canvas_course_hidden
and canvas_course_not_found (404).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import CanvasError, CanvasNotFound
from app.core.http_utils import require_time_zone
from app.crud import canvas_data_crud, canvas_link_crud, canvas_sample
from app.crud.canvas_crud import get_connection
from app.crud.canvas_fetch import (
    feed_upcoming,
    feed_zone,
    fetch_announcements,
    fetch_assignments,
    fetch_courses,
    fetch_feed,
    fetch_modules,
    fetch_overview,
    upcoming_window,
)
from app.database.database import get_db
from app.routers.canvas_errors import read_http_error
from app.schemas.canvas_schema import (
    CanvasAnnouncementsOut,
    CanvasAssignmentsOut,
    CanvasCoursesOut,
    CanvasLinkedSyncOut,
    CanvasModulesOut,
    CanvasOverviewOut,
)

router = APIRouter(prefix="/canvas", tags=["canvas"])

_TZ = Query("UTC", description="The caller's IANA time zone, which decides what 'today' is.")
_REFRESH = Query(False, description="Fetch from Canvas even if the cached data is fresh (at most every 30 seconds).")
_COURSE_ID = Path(ge=1)


def _read(db: Session, current_user: UserInfo, *args, **kwargs) -> canvas_data_crud.Read:
    try:
        return canvas_data_crud.read(db, current_user.id, *args, now=datetime.now(UTC), **kwargs)
    except CanvasNotFound as exc:
        if kwargs.get("course_id") is None:
            raise read_http_error(exc) from None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_course_not_found") from None
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise read_http_error(exc) from None


def _freshness(result: canvas_data_crud.Read) -> dict:
    return {"fetched_at": result.fetched_at, "busy": result.busy, "stale": result.stale}


def _sample() -> dict:
    return {"fetched_at": datetime.now(UTC), "sample": True}


def _no_sample_course() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_course_not_found")

@router.get("/overview", response_model=CanvasOverviewOut)
def read_canvas_overview(
    tz: str = _TZ,
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Work due from the start of today through the next 7 days, soonest first, missing and recently graded work.

    A calendar feed gives the upcoming work only.
    """
    zone = require_time_zone(tz)
    today = datetime.now(zone).date()
    start, end = upcoming_window(today, zone)
    if current_user.is_anonymous:
        return CanvasOverviewOut(**_sample(), start=start, end=end, **canvas_sample.overview(today, zone, start, end))
    connection = get_connection(db, current_user.id)
    if connection is not None and connection.connection_type == "feed":
        result = _read(db, current_user, "feed", (), fetch_feed, refresh=refresh)
        upcoming = feed_upcoming(result.data, feed_zone(connection), start, end)
        return CanvasOverviewOut(
            **_freshness(result), start=start, end=end, missing=[], recently_graded=[],
            upcoming=[item for item in upcoming if item.course_id not in result.hidden_course_ids],
        )
    result = _read(
        db, current_user, "overview", (zone.key, today.isoformat()),
        fetch_overview(start, end), refresh=refresh,
    )
    shown = {key: [item for item in items if item.course_id not in result.hidden_course_ids]
             for key, items in result.data.items()}
    return CanvasOverviewOut(**_freshness(result), start=start, end=end, **shown)


@router.get("/courses", response_model=CanvasCoursesOut)
def read_canvas_courses(
    include_hidden: bool = Query(False, description="Also list courses unticked under Courses to show, for Settings."),
    refresh: bool = _REFRESH,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active courses with the current score and grade, or grades_hidden when the instructor hides them."""
    if current_user.is_anonymous:
        return CanvasCoursesOut(**_sample(), courses=canvas_sample.courses())
    result = _read(db, current_user, "courses", (), fetch_courses, refresh=refresh)
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
        db, current_user, "modules", (course_id,), fetch_modules(course_id),
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
        db, current_user, "assignments", (course_id,), fetch_assignments(course_id),
        refresh=refresh, course_id=course_id,
    )
    return CanvasAssignmentsOut(**_freshness(result), course_id=course_id, assignments=result.data)


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
    courses = _read(db, current_user, "courses", (), fetch_courses)
    course_ids = sorted(course.id for course in courses.data)
    result = _read(
        db, current_user, "announcements", (today.isoformat(), tuple(course_ids)),
        fetch_announcements(course_ids, today), refresh=refresh,
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
    """For each open task added from Canvas: whether it's submitted, and its due date moved to Canvas's if changed.

    Tasks are never completed here; the frontend offers that when submitted is true.
    """
    zone = require_time_zone(tz)
    now = datetime.now(UTC)
    if current_user.is_anonymous:
        results = canvas_link_crud.sync_sample(db, current_user.id, zone, now.astimezone(zone).date())
        return CanvasLinkedSyncOut(**_sample(), results=results)
    try:
        result = canvas_link_crud.sync(db, current_user.id, zone, now, refresh=refresh)
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise read_http_error(exc) from None
    return CanvasLinkedSyncOut(**result)
