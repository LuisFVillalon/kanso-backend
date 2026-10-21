"""
Canvas LMS connection endpoints.

GET    /canvas/connection       — the masked connection status, never the token
PUT    /canvas/connection       — connect, replace or reconnect: checks the token with Canvas, then stores it encrypted
POST   /canvas/connection/test  — re-checks the stored token with Canvas
PATCH  /canvas/connection       — changes settings (the courses to hide)
DELETE /canvas/connection       — hard-deletes the connection and its token

Demo sandboxes are refused with 403: they'd be handing a real Canvas token
to a throwaway account. Canvas-specific failures return a stable code as
the detail (see core/canvas_client.py), so the frontend can show the right
message: canvas_url_invalid, canvas_not_canvas, canvas_token_invalid,
canvas_forbidden (422), canvas_account_changed (409), canvas_not_connected (404),
canvas_unavailable (502) and canvas_not_configured (503, the server has no
CANVAS_TOKEN_KEY).
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import CanvasError, CanvasUnavailable
from app.core.rate_limit import RateLimiter
from app.crud.canvas_crud import (
    CanvasAccountChanged,
    check_connection,
    connect,
    connection_out,
    delete_connection,
    get_connection,
    update_connection,
)
from app.database.database import get_db
from app.schemas.canvas_schema import CanvasConnect, CanvasConnectionOut, CanvasConnectionUpdate

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
