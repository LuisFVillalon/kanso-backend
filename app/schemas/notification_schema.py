import re
from datetime import date
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, Field

from app.schemas.activity_schema import UtcDatetime
from app.schemas.limits import NOTIFICATION_IDS_MAX, REMINDER_MINUTES_MAX

# Every alert type. A new one (Canvas alerts) is added here; stored
# preferences only hold overrides, so existing rows need no migration.
NotificationType = Literal["task_reminder", "final_warning", "mood_reminder", "mood_missed"]
NOTIFICATION_TYPES: tuple[str, ...] = get_args(NotificationType)
# Types no longer generated. Older clients may still send them as switches or
# ids, which are accepted and ignored.
RetiredNotificationType = Literal["due_today", "overdue", "habit_reminder"]
RETIRED_NOTIFICATION_TYPES: tuple[str, ...] = get_args(RetiredNotificationType)

# "{type}:{subject}:{occurrence}", where the occurrence starts with its local
# date, e.g. "mood_missed:mood:2026-10-20" or "final_warning:task-12:2026-10-20T16:00".
_ID_PATTERN = re.compile(
    r"(?P<type>[a-z_]+):(?P<subject>[a-z]+(?:-\d+)?):(?P<day>\d{4}-\d{2}-\d{2})(?:T(?:[01]\d|2[0-3]):[0-5]\d)?"
)


def notification_occurs_on(notification_id: str) -> date:
    """The local date of a notification id's occurrence. Raises ValueError if the id is malformed."""
    match = _ID_PATTERN.fullmatch(notification_id)
    if not match or match["type"] not in NOTIFICATION_TYPES + RETIRED_NOTIFICATION_TYPES:
        raise ValueError(f"Not a notification id: {notification_id!r}")
    return date.fromisoformat(match["day"])


def _check_id(value: str) -> str:
    notification_occurs_on(value)
    return value


NotificationId = Annotated[str, Field(max_length=100), AfterValidator(_check_id)]
# Local wall-clock time, as the profile stores day_start_time and shutoff_time.
HourMinute = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
# How long before "Call it a day" the mood check-in reminder fires; under a
# day, since the reminder is for that day.
MoodReminderMinutes = Annotated[int, Field(ge=1, le=24 * 60 - 1)]


class Notification(BaseModel):
    id: str
    type: NotificationType
    title: str
    body: str
    # When it was delivered: its fire time, or the end of quiet hours if it
    # fired during them.
    fire_at: UtcDatetime
    # The tasks it is about. habit_ids is always empty now that no alert is
    # about habits, and is kept for older clients.
    task_ids: list[int] = []
    habit_ids: list[int] = []
    # The local days a mood check-in is due or missing for.
    dates: list[date] = []
    read: bool = False


class NotificationPreferences(BaseModel):
    # Every type, on unless switched off.
    types: dict[NotificationType, bool]
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    default_reminder_minutes: int
    mood_reminder_minutes: MoodReminderMinutes


class NotificationPreferencesUpdate(BaseModel):
    # Only the types given change; retired types are ignored.
    types: dict[NotificationType | RetiredNotificationType, bool] | None = None
    # Both or neither; send both as null to turn quiet hours off.
    quiet_hours_start: HourMinute | None = None
    quiet_hours_end: HourMinute | None = None
    # null restores the built-in default.
    default_reminder_minutes: int | None = Field(None, ge=1, le=REMINDER_MINUTES_MAX)
    mood_reminder_minutes: MoodReminderMinutes | None = None


class NotificationIds(BaseModel):
    ids: list[NotificationId] = Field(max_length=NOTIFICATION_IDS_MAX)
