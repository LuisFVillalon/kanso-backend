import re
from datetime import date
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, Field

from app.schemas.activity_schema import UtcDatetime
from app.schemas.limits import NOTIFICATION_IDS_MAX, REMINDER_MINUTES_MAX

# Every alert type. A new one (a mood check-in, Canvas alerts) is added here;
# stored preferences only hold overrides, so existing rows need no migration.
NotificationType = Literal["due_today", "task_reminder", "final_warning", "overdue", "habit_reminder"]
NOTIFICATION_TYPES: tuple[str, ...] = get_args(NotificationType)

# "{type}:{subject}:{occurrence}", where the occurrence starts with its local
# date, e.g. "due_today:tasks:2026-10-20" or "overdue:task-12:2026-10-20T16:00".
_ID_PATTERN = re.compile(
    r"(?P<type>[a-z_]+):(?P<subject>[a-z]+(?:-\d+)?):(?P<day>\d{4}-\d{2}-\d{2})(?:T(?:[01]\d|2[0-3]):[0-5]\d)?"
)


def notification_occurs_on(notification_id: str) -> date:
    """The local date of a notification id's occurrence. Raises ValueError if the id is malformed."""
    match = _ID_PATTERN.fullmatch(notification_id)
    if not match or match["type"] not in NOTIFICATION_TYPES:
        raise ValueError(f"Not a notification id: {notification_id!r}")
    return date.fromisoformat(match["day"])


def _check_id(value: str) -> str:
    notification_occurs_on(value)
    return value


NotificationId = Annotated[str, Field(max_length=100), AfterValidator(_check_id)]
# Local wall-clock time, as the profile stores day_start_time and shutoff_time.
HourMinute = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class Notification(BaseModel):
    id: str
    type: NotificationType
    title: str
    body: str
    # When it was delivered: its fire time, or the end of quiet hours if it
    # fired during them.
    fire_at: UtcDatetime
    # The tasks or habits it is about.
    task_ids: list[int] = []
    habit_ids: list[int] = []
    read: bool = False


class NotificationPreferences(BaseModel):
    # Every type, on unless switched off.
    types: dict[NotificationType, bool]
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    default_reminder_minutes: int


class NotificationPreferencesUpdate(BaseModel):
    # Only the types given change.
    types: dict[NotificationType, bool] | None = None
    # Both or neither; send both as null to turn quiet hours off.
    quiet_hours_start: HourMinute | None = None
    quiet_hours_end: HourMinute | None = None
    # null restores the built-in default.
    default_reminder_minutes: int | None = Field(None, ge=1, le=REMINDER_MINUTES_MAX)


class NotificationIds(BaseModel):
    ids: list[NotificationId] = Field(max_length=NOTIFICATION_IDS_MAX)
