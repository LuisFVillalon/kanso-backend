"""
The user's Canvas connection: connect or replace, test, change settings, remove.

A connection is either an access token or the user's calendar feed link
(core/canvas_feed.py); connecting one replaces the other. A token is checked
against Canvas (GET /users/self), and a feed is fetched and parsed, before
anything is stored, so a failed connect or replace leaves the existing
connection as it was. Replacing it with a token for a different Canvas account or school is
refused with canvas_account_changed unless the caller confirms it, and then
the per-course settings are reset, since course ids belong to the old
account. Any 401 from Canvas, or a token past its expiry date, marks the
connection invalid so the frontend can ask the user to reconnect. Removing
the connection deletes the row outright, with no trash retention.
"""

import re
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import canvas_feed, crypto
from app.core.canvas_client import (
    CanvasClient,
    CanvasError,
    CanvasTokenInvalid,
    normalize_base_url,
)
from app.models.canvas_connection_model import CanvasConnection
from app.schemas.canvas_schema import CanvasCapabilities, CanvasConnectionOut
from app.schemas.limits import CANVAS_TOKEN_MAX, CANVAS_URL_MAX

# Canvas tokens are printable ASCII with no spaces, e.g. "7~" + 64 characters.
_TOKEN = re.compile(rf"[\x21-\x7e]{{16,{CANVAS_TOKEN_MAX}}}")

CAPABILITIES = {
    "token": CanvasCapabilities(
        upcoming=True, missing=True, recently_graded=True, courses=True, modules=True, assignments=True,
        announcements=True, link_tasks=True, submission_status=True,
    ),
    # A feed has the user's items and their dates, and nothing else.
    "feed": CanvasCapabilities(upcoming=True, link_tasks=True),
}


class CanvasAccountChanged(CanvasError):
    code = "canvas_account_changed"


def get_connection(db: Session, user_id: str) -> CanvasConnection | None:
    return db.query(CanvasConnection).filter(CanvasConnection.user_id == user_id).first()


def expired(row: CanvasConnection, now: datetime) -> bool:
    return row.token_expires_at is not None and row.token_expires_at <= now


def connection_out(row: CanvasConnection | None, now: datetime) -> CanvasConnectionOut:
    if row is None:
        return CanvasConnectionOut(connected=False)
    feed = row.connection_type == "feed"
    return CanvasConnectionOut(
        connected=True,
        connection_type=row.connection_type,
        capabilities=CAPABILITIES[row.connection_type],
        status="invalid" if expired(row, now) else row.status,
        base_url=row.base_url,
        token_last4=row.token_last4,
        feed_url_masked=canvas_feed.masked(row.base_url, row.token_last4) if feed else None,
        canvas_user_id=row.canvas_user_id,
        canvas_user_name=row.canvas_user_name,
        time_zone=row.time_zone,
        token_expires_at=row.token_expires_at,
        hidden_course_ids=row.hidden_course_ids or [],
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_synced_at=row.last_synced_at,
        last_error_at=row.last_error_at,
    )


def decrypt_token(row: CanvasConnection) -> str:
    """The stored secret: the access token, or the feed URL for a feed connection."""
    return crypto.decrypt(
        crypto.Encrypted(ciphertext=row.token_ciphertext, nonce=row.token_nonce, key_version=row.key_version),
        row.user_id,
    )


def connect(
    db: Session, user_id: str, base_url: str, token: str, allow_account_change: bool = False
) -> CanvasConnection:
    """Check the token with Canvas, then store it encrypted, creating or replacing the connection.

    Raises a CanvasError (nothing is stored) if the address or token is
    rejected, Canvas can't be reached, or the token belongs to a different
    account than the one connected and allow_account_change is false.
    """
    token = token.strip()
    if not _TOKEN.fullmatch(token):
        raise CanvasTokenInvalid("That doesn't look like a Canvas access token.")
    base_url = normalize_base_url(base_url)
    # Fail on a missing key before the token is sent anywhere.
    crypto.check_key()

    client = CanvasClient(base_url, token)
    account = client.get_self()
    expires_at = client.get_token_expiry()
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    encrypted = crypto.encrypt(token, user_id)
    try:
        return _store(db, user_id, base_url, token, encrypted, account, expires_at, allow_account_change)
    except IntegrityError:
        db.rollback()
        return _store(db, user_id, base_url, token, encrypted, account, expires_at, allow_account_change)


def _store(
    db: Session,
    user_id: str,
    base_url: str,
    token: str,
    encrypted: crypto.Encrypted,
    account: dict,
    expires_at: datetime | None,
    allow_account_change: bool,
) -> CanvasConnection:
    row = get_connection(db, user_id)
    account_changed = row is not None and (
        row.base_url != base_url or (row.canvas_user_id is not None and row.canvas_user_id != account["id"])
    )
    if account_changed and not allow_account_change:
        raise CanvasAccountChanged("The token belongs to a different Canvas account.")

    if row is None:
        row = CanvasConnection(user_id=user_id, hidden_course_ids=[])
        db.add(row)
    elif account_changed:
        row.hidden_course_ids = []
    row.connection_type = "token"
    row.base_url = base_url
    row.token_ciphertext = encrypted.ciphertext
    row.token_nonce = encrypted.nonce
    row.key_version = encrypted.key_version
    row.token_last4 = token[-4:]
    row.canvas_user_id = account["id"]
    row.canvas_user_name = account["name"]
    row.time_zone = None
    row.token_expires_at = expires_at
    row.status = "active"
    row.last_error_at = None
    db.commit()
    db.refresh(row)
    return row


def connect_feed(
    db: Session, user_id: str, feed_url: str, time_zone: str = "UTC", allow_account_change: bool = False
) -> CanvasConnection:
    """Fetch and parse the calendar feed, then store its URL encrypted, creating or replacing the connection.

    time_zone, an IANA zone, is the one the feed's all-day items are due in.
    It is stored only when the connection has none yet, so a reconnect keeps it.

    Raises a CanvasError (nothing is stored) if the link isn't a Canvas
    calendar feed, Canvas doesn't know it, it can't be reached or read, or
    the connection is to another school and allow_account_change is false.
    """
    if len(feed_url) > CANVAS_URL_MAX:
        raise canvas_feed.CanvasFeedUrlInvalid("That isn't a Canvas calendar feed link.")
    feed = canvas_feed.normalize_feed_url(feed_url)
    # Fail on a missing key before the feed is fetched.
    crypto.check_key()

    canvas_feed.CanvasFeed(feed.url).events()

    encrypted = crypto.encrypt(feed.url, user_id)
    try:
        return _store_feed(db, user_id, feed, encrypted, time_zone, allow_account_change)
    except IntegrityError:
        db.rollback()
        return _store_feed(db, user_id, feed, encrypted, time_zone, allow_account_change)


def _store_feed(
    db: Session,
    user_id: str,
    feed: canvas_feed.FeedUrl,
    encrypted: crypto.Encrypted,
    time_zone: str,
    allow_account_change: bool,
) -> CanvasConnection:
    row = get_connection(db, user_id)
    account_changed = row is not None and row.base_url != feed.base_url
    if account_changed and not allow_account_change:
        raise CanvasAccountChanged("The feed belongs to a different Canvas school.")

    if row is None:
        row = CanvasConnection(user_id=user_id, hidden_course_ids=[])
        db.add(row)
    elif account_changed:
        row.hidden_course_ids = []
    row.connection_type = "feed"
    row.base_url = feed.base_url
    row.token_ciphertext = encrypted.ciphertext
    row.token_nonce = encrypted.nonce
    row.key_version = encrypted.key_version
    row.token_last4 = feed.last4
    row.canvas_user_id = None
    row.canvas_user_name = None
    if row.time_zone is None:
        row.time_zone = time_zone
    row.token_expires_at = None
    row.status = "active"
    row.last_error_at = None
    db.commit()
    db.refresh(row)
    return row


def check_connection(db: Session, user_id: str, now: datetime) -> CanvasConnection | None:
    """Check the stored token with Canvas, or fetch the feed, and record the outcome. None if not connected.

    A rejected, undecryptable or expired token marks the connection invalid
    and is returned, not raised; an expired one isn't sent to Canvas at all.
    Any other CanvasError (e.g. Canvas unreachable) marks it "error" and is
    raised after the change is committed.
    """
    row = get_connection(db, user_id)
    if row is None:
        return None
    try:
        if expired(row, now):
            raise CanvasTokenInvalid("The token has expired.")
        try:
            token = decrypt_token(row)
        except crypto.DecryptionError:
            raise CanvasTokenInvalid("The stored token can't be decrypted.") from None
        if row.connection_type == "feed":
            canvas_feed.CanvasFeed(token).events()
            account = None
        else:
            account = CanvasClient(row.base_url, token).get_self()
    except CanvasTokenInvalid:
        row.status = "invalid"
        row.last_error_at = now
    except CanvasError:
        row.status = "error"
        row.last_error_at = now
        db.commit()
        raise
    else:
        row.status = "active"
        if account is not None:
            row.canvas_user_name = account["name"]
    db.commit()
    db.refresh(row)
    return row


def update_connection(db: Session, user_id: str, hidden_course_ids: list[int]) -> CanvasConnection | None:
    """Set the courses hidden under "Courses to show". None if not connected."""
    row = get_connection(db, user_id)
    if row is None:
        return None
    row.hidden_course_ids = sorted(set(hidden_course_ids))
    db.commit()
    db.refresh(row)
    return row


def delete_connection(db: Session, user_id: str) -> None:
    """Hard-delete the connection, token included. Idempotent."""
    db.query(CanvasConnection).filter(CanvasConnection.user_id == user_id).delete(synchronize_session=False)
    db.commit()
