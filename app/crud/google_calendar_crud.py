"""
The user's read-only Google Calendar import: connecting, syncing, tags.

A sync lists the user's calendars, then each selected calendar's changes
since its sync token; a full sync (no token, an expired one, or the rolling
window due to move forward) replaces the window's events instead. Each
calendar commits on its own, so a failure partway keeps what finished.
Repeating events arrive as single instances, one row each, and a row keeps
its tags whenever Google still lists it. Kanso's own changes to imported
events (crud/google_event_crud.py) survive syncs too: an edit's details stay
until Google changes the event after it, and a "following" or "all" change
reaches the instances later syncs import.

Reads refresh the import when it is stale (refresh_if_stale). That needs no
background worker, and a failure only lands in last_error: the calendar
endpoints still answer with what is stored.
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from time import monotonic

from fastapi import HTTPException, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import crypto, google_calendar
from app.core.google_calendar import (
    GoogleCalendarClient,
    GoogleError,
    GoogleNoRefreshToken,
    GoogleNotAccessible,
    GoogleScopesMissing,
    GoogleSyncTokenExpired,
    ImportedEvent,
)
from app.crud import google_event_crud
from app.crud.base import get_owned
from app.models.google_calendar_model import (
    GoogleCalendar,
    GoogleCalendarConnection,
    GoogleCalendarEvent,
    GoogleOAuthState,
)
from app.models.tag_model import Tag
from app.schemas.google_calendar_schema import GoogleCalendarOut, GoogleCalendarStatus

log = logging.getLogger(__name__)

_STATE_LIFETIME = timedelta(minutes=10)
# How old the import may get before a read refreshes it.
STALE_AFTER = timedelta(minutes=15)
# The closest together a user's "Sync now" can run.
_MANUAL_SYNC_GAP = timedelta(seconds=30)
# Seconds a read waits on Google, and a connect or "Sync now".
_READ_BUDGET = 8
_FULL_BUDGET = 20
# The imported window. Reports reach back a year, and forward a year even
# when the window is at its oldest.
_HISTORY = timedelta(days=366)
_HORIZON = timedelta(days=396)
_MOVE_WINDOW_AFTER = timedelta(days=30)
_FIELDS = tuple(f for f in ImportedEvent.__dataclass_fields__ if f not in ("google_event_id", "updated"))
# The details a kanso edit changes, which a sync leaves alone until Google changes the event after it.
_EDITABLE = {"title", "description", "all_day", "start_date", "end_date", "starts_at", "ends_at", "time_zone"}


# ── The connection ───────────────────────────────────────────────────────────

def get_connection(db: Session, user_id: str) -> GoogleCalendarConnection | None:
    return db.query(GoogleCalendarConnection).filter(GoogleCalendarConnection.user_id == user_id).first()


def status_out(row: GoogleCalendarConnection | None, configured: bool) -> GoogleCalendarStatus:
    if row is None:
        return GoogleCalendarStatus(configured=configured, connected=False)
    return GoogleCalendarStatus(
        configured=configured,
        connected=True,
        email=row.email,
        last_synced_at=row.last_synced_at,
        last_error=row.last_error,
    )


def _aad(user_id: str) -> str:
    # Tied to the feature as well as the user, so a Canvas token's ciphertext can't be swapped in.
    return f"google-calendar:{user_id}"


def _refresh_token(row: GoogleCalendarConnection) -> str:
    return crypto.decrypt(
        crypto.Encrypted(ciphertext=row.token_ciphertext, nonce=row.token_nonce, key_version=row.key_version),
        _aad(row.user_id),
    )


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def start_connect(db: Session, user_id: str, return_origin: str) -> str:
    """Remember a fresh OAuth state for this user and return Google's consent URL."""
    state = secrets.token_urlsafe(32)
    values = {
        "state_hash": _state_hash(state),
        "return_origin": return_origin,
        "expires_at": datetime.now(UTC) + _STATE_LIFETIME,
    }
    db.execute(
        insert(GoogleOAuthState)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(index_elements=[GoogleOAuthState.user_id], set_=values)
    )
    db.commit()
    return google_calendar.authorization_url(state)


def state_origin(db: Session, state: str) -> str | None:
    """The frontend a live state came from, or None. Leaves the state unspent."""
    return db.execute(
        select(GoogleOAuthState.return_origin).where(
            GoogleOAuthState.state_hash == _state_hash(state),
            GoogleOAuthState.expires_at > datetime.now(UTC),
        )
    ).scalar_one_or_none()


def consume_state(db: Session, state: str, user_id: str) -> bool:
    """Spend a state issued to this user. A state works once, and another user can't spend it."""
    row = db.execute(
        delete(GoogleOAuthState)
        .where(GoogleOAuthState.state_hash == _state_hash(state), GoogleOAuthState.user_id == user_id)
        .returning(GoogleOAuthState.expires_at)
    ).first()
    db.commit()
    return row is not None and row.expires_at > datetime.now(UTC)


def finish_connect(db: Session, user_id: str, code: str) -> None:
    """Trade the code for tokens, store the refresh token encrypted and run the first sync.

    Raises a GoogleError with nothing stored if Google refuses the code or
    grants less than kanso needs. A failed first sync still connects, with
    last_error set.
    """
    tokens = google_calendar.exchange_code(code)
    if not tokens.refresh_token:
        raise GoogleNoRefreshToken("Google sent no refresh token.")
    if not set(google_calendar.SCOPES) <= tokens.scopes:
        google_calendar.revoke(tokens.refresh_token)
        raise GoogleScopesMissing("Google didn't grant every permission.")
    encrypted = crypto.encrypt(tokens.refresh_token, _aad(user_id))
    values = {
        "token_ciphertext": encrypted.ciphertext,
        "token_nonce": encrypted.nonce,
        "key_version": encrypted.key_version,
        "last_attempt_at": None,
        "last_error": None,
    }
    db.execute(
        insert(GoogleCalendarConnection)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(
            index_elements=[GoogleCalendarConnection.user_id],
            set_={**values, "updated_at": datetime.now(UTC)},
        )
    )
    db.commit()
    _run_sync(db, user_id, _MANUAL_SYNC_GAP, _FULL_BUDGET)


def disconnect(db: Session, user_id: str) -> None:
    """Revoke the token at Google where possible and delete it and everything imported."""
    row = get_connection(db, user_id)
    if row is not None:
        try:
            refresh_token = _refresh_token(row)
        except (crypto.EncryptionKeyError, crypto.DecryptionError):
            log.warning("GOOGLE_CALENDAR | couldn't read a token to revoke it")
            refresh_token = None
        db.commit()
        if refresh_token is not None:
            google_calendar.revoke(refresh_token)
        # Calendars, events and their tags cascade from the connection.
        db.delete(row)
    db.execute(delete(GoogleOAuthState).where(GoogleOAuthState.user_id == user_id))
    db.commit()


# ── Sync ─────────────────────────────────────────────────────────────────────

def sync_now(db: Session, user_id: str) -> None:
    """A sync the user asked for. Any failure is in the connection's last_error."""
    _run_sync(db, user_id, _MANUAL_SYNC_GAP, _FULL_BUDGET)


def refresh_if_stale(db: Session, user_id: str) -> None:
    """For read paths: bring the import up to date if it's stale. Never raises."""
    try:
        if google_calendar.configured():
            _run_sync(db, user_id, STALE_AFTER, _READ_BUDGET)
    except Exception as exc:
        # The class name only: the message could hold request details.
        log.error("GOOGLE_CALENDAR | refresh failed: %s", type(exc).__name__)
        db.rollback()


def _claim(db: Session, user_id: str, min_age: timedelta) -> bool:
    """Mark a sync as started unless one started within min_age.

    One atomic update, so it paces retries after a failure and keeps two
    syncs of one user from running at once, without holding a transaction
    open across calls to Google.
    """
    now = datetime.now(UTC)
    claimed = db.execute(
        update(GoogleCalendarConnection)
        .where(
            GoogleCalendarConnection.user_id == user_id,
            or_(
                GoogleCalendarConnection.last_attempt_at.is_(None),
                GoogleCalendarConnection.last_attempt_at <= now - min_age,
            ),
        )
        .values(last_attempt_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return bool(claimed)


def _run_sync(db: Session, user_id: str, min_age: timedelta, budget: float) -> None:
    if not _claim(db, user_id, min_age):
        return
    try:
        _sync(db, user_id, monotonic() + budget)
    except GoogleError as exc:
        _record_failure(db, user_id, exc.code)
    except (crypto.EncryptionKeyError, crypto.DecryptionError):
        _record_failure(db, user_id, "google_token_unreadable")
    except Exception as exc:
        log.error("GOOGLE_CALENDAR | sync failed: %s", type(exc).__name__)
        _record_failure(db, user_id, "google_sync_failed")


def _record_failure(db: Session, user_id: str, code: str) -> None:
    db.rollback()
    row = get_connection(db, user_id)
    if row is not None:
        row.last_error = code
        db.commit()


def _sync(db: Session, user_id: str, deadline: float) -> None:
    connection = get_connection(db, user_id)
    refresh_token = _refresh_token(connection)
    db.commit()
    tokens = google_calendar.refresh_access_token(refresh_token, deadline)
    if tokens.refresh_token:
        # Google may rotate it.
        encrypted = crypto.encrypt(tokens.refresh_token, _aad(user_id))
        connection.token_ciphertext = encrypted.ciphertext
        connection.token_nonce = encrypted.nonce
        connection.key_version = encrypted.key_version
        db.commit()
    client = GoogleCalendarClient(tokens.access_token, deadline)
    calendars = _sync_calendars(db, connection, client.calendars())
    db.commit()
    now = datetime.now(UTC)
    for calendar in calendars:
        if not calendar.selected:
            continue
        try:
            _sync_events(db, calendar, client, now)
        except GoogleNotAccessible:
            # Unshared or removed since the list was read: the others can still sync.
            continue
        db.commit()
    connection.last_synced_at = datetime.now(UTC)
    connection.last_error = None
    db.commit()


def _sync_calendars(db: Session, connection: GoogleCalendarConnection, listed: list[dict]) -> list[GoogleCalendar]:
    """Bring the stored calendars in line with Google's list. Settings stay; new calendars
    start selected if they are shown in Google Calendar."""
    existing = {
        calendar.google_calendar_id: calendar
        for calendar in db.query(GoogleCalendar).filter(GoogleCalendar.user_id == connection.user_id)
    }
    current: list[GoogleCalendar] = []
    for item in listed:
        google_id = item.get("id")
        if not google_id:
            continue
        calendar = existing.pop(google_id, None)
        if calendar is None:
            calendar = GoogleCalendar(
                user_id=connection.user_id,
                google_calendar_id=google_id,
                selected=bool(item.get("selected") or item.get("primary")),
            )
            db.add(calendar)
        calendar.name = item.get("summaryOverride") or item.get("summary") or google_id
        calendar.color = (item.get("backgroundColor") or "")[:32] or None
        calendar.time_zone = google_calendar.known_zone(item.get("timeZone")) or "UTC"
        if item.get("primary"):
            connection.email = google_id
        current.append(calendar)
    for gone in existing.values():
        db.delete(gone)
    db.flush()
    return current


def _sync_events(db: Session, calendar: GoogleCalendar, client: GoogleCalendarClient, now: datetime) -> None:
    window_end = now + _HORIZON
    window_is_fresh = calendar.synced_through is not None and calendar.synced_through >= window_end - _MOVE_WINDOW_AFTER
    google_id, sync_token = calendar.google_calendar_id, calendar.sync_token
    db.commit()
    if sync_token and window_is_fresh:
        try:
            changes = client.events(google_id, sync_token=sync_token)
        except GoogleSyncTokenExpired:
            pass
        else:
            _apply_changes(db, calendar, changes.items)
            calendar.sync_token = changes.next_sync_token
            return
    window_start = now - _HISTORY
    listing = client.events(google_id, time_min=window_start, time_max=window_end)
    _replace_window(db, calendar, listing.items, window_start, window_end)
    calendar.sync_token = listing.next_sync_token
    calendar.synced_through = window_end


def _write(
    db: Session, calendar: GoogleCalendar, event: ImportedEvent, row: GoogleCalendarEvent | None
) -> GoogleCalendarEvent | None:
    """Store the event in its row, or a new one. Returns the row if it's new.

    A row kanso edited keeps its details unless Google changed the event after the edit.
    """
    new = row is None
    if new:
        row = GoogleCalendarEvent(
            user_id=calendar.user_id, calendar_id=calendar.id, google_event_id=event.google_event_id
        )
        db.add(row)
    keep_edit = row.edited_at is not None and (event.updated is None or event.updated <= row.edited_at)
    for field in _FIELDS:
        if not (keep_edit and field in _EDITABLE):
            setattr(row, field, getattr(event, field))
    row.google_starts_at = event.starts_at
    if not keep_edit:
        row.edited_at = None
    return row if new else None


def _write_all(db: Session, calendar: GoogleCalendar, events: dict, rows: dict) -> None:
    """Store each event in its row, then give new instances the changes made to their series."""
    new_rows = []
    for google_id, event in events.items():
        row = _write(db, calendar, event, rows.get(google_id))
        if row is not None:
            new_rows.append((row, event.updated))
    google_event_crud.apply_overrides(db, calendar.user_id, new_rows)


def _parse_all(calendar: GoogleCalendar, items: list[dict]) -> dict[str, ImportedEvent | None]:
    """Each item by Google id, None for one that's cancelled or can't be placed. The last mention wins."""
    return {
        item["id"]: google_calendar.parse_event(item, calendar.time_zone)
        for item in items
        if item.get("id")
    }


def _apply_changes(db: Session, calendar: GoogleCalendar, items: list[dict]) -> None:
    parsed = _parse_all(calendar, items)
    # An event moved past the window is picked up again when the window moves.
    gone = [
        google_id for google_id, event in parsed.items()
        if event is None or event.starts_at >= calendar.synced_through
    ]
    if gone:
        # A cancelled repeating event can arrive as just its parent.
        db.execute(
            delete(GoogleCalendarEvent).where(
                GoogleCalendarEvent.calendar_id == calendar.id,
                or_(
                    GoogleCalendarEvent.google_event_id.in_(gone),
                    GoogleCalendarEvent.recurring_event_id.in_(gone),
                ),
            )
        )
    changed = {google_id: event for google_id, event in parsed.items() if google_id not in gone}
    if changed:
        rows = {
            row.google_event_id: row
            for row in db.query(GoogleCalendarEvent).filter(
                GoogleCalendarEvent.calendar_id == calendar.id,
                GoogleCalendarEvent.google_event_id.in_(changed),
            )
        }
        _write_all(db, calendar, changed, rows)


def _replace_window(
    db: Session, calendar: GoogleCalendar, items: list[dict], window_start: datetime, window_end: datetime
) -> None:
    """Make the window match a full listing. Rows Google still lists keep their tags."""
    events = {google_id: event for google_id, event in _parse_all(calendar, items).items() if event is not None}
    rows = {
        row.google_event_id: row
        for row in db.query(GoogleCalendarEvent).filter(GoogleCalendarEvent.calendar_id == calendar.id)
    }
    _write_all(db, calendar, events, rows)
    removed = [
        row.id for google_id, row in rows.items()
        if google_id not in events and row.ends_at > window_start and row.google_starts_at < window_end
    ]
    if removed:
        db.execute(delete(GoogleCalendarEvent).where(GoogleCalendarEvent.id.in_(removed)))


# ── Calendars and tags ───────────────────────────────────────────────────────

def _calendar_out(calendar: GoogleCalendar) -> GoogleCalendarOut:
    tag = calendar.default_tag
    return GoogleCalendarOut(
        id=calendar.id,
        name=calendar.name,
        color=calendar.color,
        selected=calendar.selected,
        default_tag=tag if tag is not None and tag.deleted_at is None else None,
    )


def list_calendars(db: Session, user_id: str) -> list[GoogleCalendarOut]:
    calendars = (
        db.query(GoogleCalendar).filter(GoogleCalendar.user_id == user_id)
        .order_by(GoogleCalendar.name, GoogleCalendar.id).all()
    )
    return [_calendar_out(calendar) for calendar in calendars]


def _tag_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")


def update_calendar(db: Session, calendar_id: int, user_id: str, changes: dict) -> GoogleCalendarOut | None:
    """Apply the settings given. Returns None if the calendar isn't the user's."""
    calendar = get_owned(db, GoogleCalendar, calendar_id, user_id)
    if calendar is None:
        return None
    if changes.get("selected") is not None:
        if changes["selected"] and not calendar.selected:
            # So its events come in on the next read instead of up to STALE_AFTER later.
            db.execute(
                update(GoogleCalendarConnection)
                .where(GoogleCalendarConnection.user_id == user_id)
                .values(last_attempt_at=None)
                .execution_options(synchronize_session=False)
            )
        calendar.selected = changes["selected"]
    if "default_tag_id" in changes:
        tag_id = changes["default_tag_id"]
        if tag_id is not None and get_owned(db, Tag, tag_id, user_id) is None:
            raise _tag_not_found()
        calendar.default_tag_id = tag_id
    db.commit()
    db.refresh(calendar)
    return _calendar_out(calendar)
