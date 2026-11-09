"""
Google Calendar, read-only import.

GET    /google-calendar/status            — configured, connected, account, last sync and last error
POST   /google-calendar/connect           — Google's consent URL for this user
GET    /google-calendar/callback          — Google's redirect target; finishes connecting, sends the browser back
DELETE /google-calendar/connection        — revokes the token where possible and deletes everything imported
GET    /google-calendar/calendars         — the account's calendars
PATCH  /google-calendar/calendars/{id}    — selects a calendar or sets its default tag
POST   /google-calendar/sync              — syncs now and returns the status
PATCH  /google-calendar/events/{id}/tags  — sets an imported event's own tags, or returns it to the default

All but the callback use the Supabase JWT. The callback is a browser redirect
from Google with no JWT: the single-use state issued to the signed-in user at
connect says whose account it is. It sends the browser to the frontend's
/settings with google_calendar=success, or google_calendar=error&reason=<code>.

Imported events reach the calendar through the existing event and activity
responses, with source "google" and read_only true. Their event_id is
negative, and is the {id} for setting their tags.

Without the Google OAuth values the app runs as usual: status says
configured=false and connect and sync answer 503 google_calendar_not_configured.
Demo sandboxes can't connect (403), and their status says configured=false.
"""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core import crypto, google_calendar
from app.core.auth import UserInfo, get_current_user
from app.core.google_calendar import GoogleError
from app.core.http_utils import allowed_origins, default_origin, require_found
from app.core.rate_limit import RateLimiter
from app.crud import google_calendar_crud as crud
from app.database.database import get_db
from app.schemas.google_calendar_schema import (
    GoogleCalendarConnectOut,
    GoogleCalendarOut,
    GoogleCalendarStatus,
    GoogleCalendarUpdate,
    GoogleEventTagsOut,
    GoogleEventTagsUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/google-calendar", tags=["google-calendar"])

# Each of these calls Google with the user's grant.
_google_call_limiter = RateLimiter(max_calls=10, period=60)


def _real_user(current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
    if current_user.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Google Calendar can't be connected to a demo sandbox. Sign up for your own account to connect it.",
        )
    return current_user


def _require_configured() -> None:
    if not google_calendar.configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="google_calendar_not_configured")


def _settings_redirect(origin: str, **query: str) -> RedirectResponse:
    return RedirectResponse(f"{origin}/settings?{urlencode(query)}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/status", response_model=GoogleCalendarStatus)
def read_status(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Whether Google Calendar can be connected, and how the connection is doing."""
    if current_user.is_anonymous:
        return crud.status_out(None, configured=False)
    return crud.status_out(crud.get_connection(db, current_user.id), google_calendar.configured())


@router.post("/connect", response_model=GoogleCalendarConnectOut)
def connect(
    request: Request,
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Start connecting: returns the URL to send the browser to."""
    _require_configured()
    _google_call_limiter.check(current_user.id)
    origin = request.headers.get("origin", "")
    return_origin = origin if origin in allowed_origins() else default_origin()
    return GoogleCalendarConnectOut(authorization_url=crud.start_connect(db, current_user.id, return_origin))


@router.get("/callback", include_in_schema=False)
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Google's redirect after the consent screen."""
    pending = crud.consume_state(db, state) if state else None
    if pending is None:
        return _settings_redirect(default_origin(), google_calendar="error", reason="google_invalid_state")
    user_id, origin = pending
    if not google_calendar.configured():
        return _settings_redirect(origin, google_calendar="error", reason="google_calendar_not_configured")
    if error or not code:
        return _settings_redirect(origin, google_calendar="error", reason="google_access_denied")
    try:
        crud.finish_connect(db, user_id, code)
    except GoogleError as exc:
        return _settings_redirect(origin, google_calendar="error", reason=exc.code)
    except crypto.EncryptionKeyError:
        log.error("GOOGLE_CALENDAR | the token encryption key is unusable")
        return _settings_redirect(origin, google_calendar="error", reason="google_calendar_not_configured")
    return _settings_redirect(origin, google_calendar="success")


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
def remove_connection(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Revoke the token at Google where possible and delete it and all imported data."""
    crud.disconnect(db, current_user.id)


@router.get("/calendars", response_model=list[GoogleCalendarOut])
def read_calendars(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """The connected account's calendars; empty if none is connected."""
    crud.refresh_if_stale(db, current_user.id)
    return crud.list_calendars(db, current_user.id)


@router.patch("/calendars/{calendar_id}", response_model=GoogleCalendarOut)
def patch_calendar(
    calendar_id: int,
    body: GoogleCalendarUpdate,
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Choose whether a calendar is imported, or set or clear (null) its default tag."""
    return require_found(
        crud.update_calendar(db, calendar_id, current_user.id, body.model_dump(exclude_unset=True)),
        "Calendar not found",
    )


@router.post("/sync", response_model=GoogleCalendarStatus)
def sync(
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Sync now. A failure is reported in the status's last_error."""
    _require_configured()
    _google_call_limiter.check(current_user.id)
    if crud.get_connection(db, current_user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="google_calendar_not_connected")
    crud.sync_now(db, current_user.id)
    return crud.status_out(crud.get_connection(db, current_user.id), configured=True)


@router.patch("/events/{event_id}/tags", response_model=GoogleEventTagsOut)
def patch_event_tags(
    event_id: int,
    body: GoogleEventTagsUpdate,
    current_user: UserInfo = Depends(_real_user),
    db: Session = Depends(get_db),
):
    """Replace an imported event's own tags. Tags stay in kanso and never reach Google."""
    return require_found(
        crud.set_event_tags(db, event_id, current_user.id, body.tag_ids, body.use_default), "Event not found"
    )
