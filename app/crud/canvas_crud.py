"""
The user's Canvas connection: connect or replace, test, change settings, remove.

A token is checked against Canvas (GET /users/self) before anything is
stored, so a failed connect or replace leaves the existing connection as it
was. Replacing it with a token for a different Canvas account or school is
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

from app.core import crypto
from app.core.canvas_client import (
    CanvasClient,
    CanvasError,
    CanvasTokenInvalid,
    normalize_base_url,
)
from app.models.canvas_connection_model import CanvasConnection
from app.schemas.canvas_schema import CanvasConnectionOut
from app.schemas.limits import CANVAS_TOKEN_MAX

# Canvas tokens are printable ASCII with no spaces, e.g. "7~" + 64 characters.
_TOKEN = re.compile(rf"[\x21-\x7e]{{16,{CANVAS_TOKEN_MAX}}}")


class CanvasAccountChanged(CanvasError):
    code = "canvas_account_changed"


def get_connection(db: Session, user_id: str) -> CanvasConnection | None:
    return db.query(CanvasConnection).filter(CanvasConnection.user_id == user_id).first()


def expired(row: CanvasConnection, now: datetime) -> bool:
    return row.token_expires_at is not None and row.token_expires_at <= now


def connection_out(row: CanvasConnection | None, now: datetime) -> CanvasConnectionOut:
    if row is None:
        return CanvasConnectionOut(connected=False)
    return CanvasConnectionOut(
        connected=True,
        status="invalid" if expired(row, now) else row.status,
        base_url=row.base_url,
        token_last4=row.token_last4,
        canvas_user_id=row.canvas_user_id,
        canvas_user_name=row.canvas_user_name,
        token_expires_at=row.token_expires_at,
        hidden_course_ids=row.hidden_course_ids or [],
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_synced_at=row.last_synced_at,
        last_error_at=row.last_error_at,
    )


def decrypt_token(row: CanvasConnection) -> str:
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
    account_changed = row is not None and (row.base_url != base_url or row.canvas_user_id != account["id"])
    if account_changed and not allow_account_change:
        raise CanvasAccountChanged("The token belongs to a different Canvas account.")

    if row is None:
        row = CanvasConnection(user_id=user_id, hidden_course_ids=[])
        db.add(row)
    elif account_changed:
        row.hidden_course_ids = []
    row.base_url = base_url
    row.token_ciphertext = encrypted.ciphertext
    row.token_nonce = encrypted.nonce
    row.key_version = encrypted.key_version
    row.token_last4 = token[-4:]
    row.canvas_user_id = account["id"]
    row.canvas_user_name = account["name"]
    row.token_expires_at = expires_at
    row.status = "active"
    row.last_error_at = None
    db.commit()
    db.refresh(row)
    return row


def check_connection(db: Session, user_id: str, now: datetime) -> CanvasConnection | None:
    """Check the stored token with Canvas and record the outcome. None if not connected.

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
