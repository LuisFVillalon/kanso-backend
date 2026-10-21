"""
Canvas LMS endpoints: the connection, and the data shown in the home-page panel.

GET    /canvas/connection       — the masked connection status, never the token
PUT    /canvas/connection       — connect, replace or reconnect: checks the token with Canvas, then stores it encrypted
POST   /canvas/connection/test  — re-checks the stored token with Canvas
PATCH  /canvas/connection       — changes settings (the courses to hide)
DELETE /canvas/connection       — hard-deletes the connection and its token

GET    /canvas/overview                        — due in the next 7 days, and missing work
GET    /canvas/courses                         — active courses with current grades
GET    /canvas/courses/{course_id}/modules     — a course's modules and their items
GET    /canvas/courses/{course_id}/assignments — a course's assignments and quizzes, with the user's submissions
GET    /canvas/announcements                   — the last 14 days of announcements

The read routes proxy Canvas through a short cache (crud/canvas_data_crud.py)
and return fetched_at, busy and stale with the data. Demo sandboxes get
read-only sample data from them (crud/canvas_sample.py), with sample=true and
no Canvas call, so the panel can be shown off.

Demo sandboxes are refused with 403 on the connection routes: they'd be
handing a real Canvas token to a throwaway account. Canvas-specific failures return a stable code as
the detail (see core/canvas_client.py), so the frontend can show the right
message: canvas_url_invalid, canvas_not_canvas, canvas_token_invalid,
canvas_forbidden (422), canvas_account_changed (409), canvas_not_connected (404),
canvas_unavailable (502) and canvas_not_configured (503, the server has no
CANVAS_TOKEN_KEY). The read routes answer canvas_token_invalid with 409
("Reconnect Canvas"), and add canvas_busy (502: Canvas is rate-limiting and
nothing is cached), canvas_course_hidden and canvas_course_not_found (404).
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
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
from app.crud import canvas_data_crud, canvas_sample
from app.crud.canvas_crud import (
    CanvasAccountChanged,
    check_connection,
    connect,
    connection_out,
    delete_connection,
    get_connection,
    update_connection,
)
from app.crud.canvas_data_crud import CanvasCourseHidden, CanvasNotConnected
from app.database.database import get_db
from app.schemas.canvas_schema import (
    CanvasAnnouncementsOut,
    CanvasAssignmentsOut,
    CanvasConnect,
    CanvasConnectionOut,
    CanvasConnectionUpdate,
    CanvasCoursesOut,
    CanvasModulesOut,
    CanvasOverviewOut,
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
    """Set the Canvas course ids hidden from the panel."""
    row = update_connection(db, current_user.id, changes.hidden_course_ids)
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


def _read_http_error(exc: CanvasError | crypto.EncryptionKeyError) -> HTTPException:
    if isinstance(exc, CanvasNotConnected):
        return _not_connected()
    if isinstance(exc, CanvasTokenInvalid):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code)
    if isinstance(exc, CanvasRateLimited):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="canvas_busy", headers={"Retry-After": "30"}
        )
    if isinstance(exc, CanvasCourseHidden):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.code)
    return _canvas_http_error(exc)


def _read(db: Session, current_user: UserInfo, *args, **kwargs) -> canvas_data_crud.Read:
    try:
        return canvas_data_crud.read(db, current_user.id, *args, now=datetime.now(UTC), **kwargs)
    except CanvasNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="canvas_course_not_found") from None
    except (CanvasError, crypto.EncryptionKeyError) as exc:
        raise _read_http_error(exc) from None


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
    """Work due from the start of today through the next 7 days, soonest first, and missing work."""
    zone = require_time_zone(tz)
    today = datetime.now(zone).date()
    start, end = canvas_data_crud.upcoming_window(today, zone)
    if current_user.is_anonymous:
        return CanvasOverviewOut(**_sample(), start=start, end=end, **canvas_sample.overview(today, zone, start, end))
    result = _read(
        db, current_user, "overview", (zone.key, today.isoformat()),
        canvas_data_crud.fetch_overview(start, end), refresh=refresh,
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
