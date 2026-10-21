from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_time_zone
from app.crud.notification_crud import (
    dismiss,
    get_notifications,
    get_preferences,
    mark_all_read,
    mark_read,
    update_preferences,
)
from app.database.database import get_db
from app.schemas.notification_schema import (
    Notification,
    NotificationIds,
    NotificationPreferences,
    NotificationPreferencesUpdate,
)

router = APIRouter()

_TZ_QUERY = Query(
    ...,
    description=(
        "The caller's IANA time zone, e.g. America/Los_Angeles (the browser's "
        "Intl.DateTimeFormat().resolvedOptions().timeZone). Due times, the day start, "
        "\"Call it a day\" and quiet hours are local times in this zone."
    ),
)


@router.get("/get-notifications", response_model=list[Notification])
def read_notifications(
    tz: str = _TZ_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the notifications that have fired for the user by now, newest first, without dismissed ones."""
    return get_notifications(db, current_user.id, require_time_zone(tz), datetime.now(UTC))


@router.post("/mark-notifications-read", status_code=status.HTTP_204_NO_CONTENT)
def mark_notifications_read(
    body: NotificationIds,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark notifications read by id."""
    mark_read(db, current_user.id, body.ids)


@router.post("/mark-all-notifications-read", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_notifications_read(
    tz: str = _TZ_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark every notification /get-notifications currently returns as read."""
    mark_all_read(db, current_user.id, require_time_zone(tz), datetime.now(UTC))


@router.post("/dismiss-notifications", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_notifications(
    body: NotificationIds,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Dismiss notifications by id, so /get-notifications leaves them out."""
    dismiss(db, current_user.id, body.ids)


@router.get("/get-notification-preferences", response_model=NotificationPreferences)
def read_notification_preferences(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's notification preferences, with defaults for anything never set."""
    return get_preferences(db, current_user.id)


@router.patch("/update-notification-preferences", response_model=NotificationPreferences)
def update_notification_preferences(
    changes: NotificationPreferencesUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the fields given: switch types on or off, set or clear quiet hours, set the default reminder lead."""
    return update_preferences(db, current_user.id, changes.model_dump(exclude_unset=True))
