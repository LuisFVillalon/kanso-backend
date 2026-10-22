"""
Canvas LMS connection endpoints.

GET    /canvas/connection       — the masked connection status and its capabilities, never the token or feed URL
PUT    /canvas/connection       — connect, replace or reconnect: checks the token with Canvas, then stores it encrypted
PUT    /canvas/connection/feed  — the same with a calendar feed link: fetches and parses it, then stores it encrypted
POST   /canvas/connection/test  — re-checks the stored token with Canvas, or fetches the feed
PATCH  /canvas/connection       — changes settings (the courses to hide)
DELETE /canvas/connection       — hard-deletes the connection and its token

Demo sandboxes are refused with 403 here: they'd be handing a real Canvas
token to a throwaway account. Failures return a stable code as the detail
(see canvas_errors.py): canvas_url_invalid, canvas_not_canvas,
canvas_token_invalid, canvas_forbidden (422), canvas_account_changed (409),
canvas_not_connected (404), canvas_feed_url_invalid and canvas_feed_invalid
(422: a feed link that isn't one, or one Canvas doesn't know),
canvas_unavailable (502) and canvas_not_configured (503, the server has no
CANVAS_TOKEN_KEY).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core import canvas_cache, crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import CanvasError
from app.core.http_utils import require_time_zone
from app.core.rate_limit import RateLimiter
from app.crud.canvas_crud import (
    check_connection,
    connect,
    connect_feed,
    connection_out,
    delete_connection,
    get_connection,
    update_connection,
)
from app.database.database import get_db
from app.routers.canvas_errors import canvas_http_error, not_connected
from app.schemas.canvas_schema import CanvasConnect, CanvasConnectionOut, CanvasConnectionUpdate, CanvasFeedConnect

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
        raise canvas_http_error(exc) from None
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
        raise canvas_http_error(exc) from None
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
        raise canvas_http_error(exc) from None
    if row is None:
        raise not_connected()
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
        raise not_connected()
    return connection_out(row, datetime.now(UTC))


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
def remove_canvas_connection(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the connection and its token. There is no trash for it."""
    delete_connection(db, current_user.id)
    canvas_cache.evict_user(current_user.id)
