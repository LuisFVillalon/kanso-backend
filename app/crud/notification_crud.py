"""
In-app notifications, computed on demand.

Nothing is scheduled or queued: each GET works out, from the user's tasks,
mood check-ins, profile and notification preferences, which alerts have
fired by now in the caller's time zone. The alerts are:

- task_reminder: a task's reminder_minutes (or the user's default) before
  its due time, until the final warning takes over.
- final_warning: 30 minutes before a task's due time, until it is due.
- mood_reminder: mood_reminder_minutes (default 60) before "Call it a day",
  but not before the day start, while the day's mood check-in isn't logged.
  Not on rest days.
- mood_missed: at the day start, listing the past week's days with no mood
  check-in, counted from the user's first one. Not on rest days.

Both mood alerts need daily check-ins on in the profile's mood settings; they
only say that a check-in is due or missing, never what one holds.

Only tasks with a due_date and due_time count. Completed, archived and
deleted tasks are left out. An alert that fires during the user's quiet
hours is held until they end, and dropped if it would no longer apply by then.

Each notification has a deterministic id, "{type}:{subject}:{occurrence}",
so the same alert gets the same id on every device and reload; moving a
task's due time makes a new occurrence. Only what the user did with a
notification (read, dismissed) is stored, in notification_states. Occurrences
older than NOTIFICATION_RETENTION are no longer generated, and their state
rows are pruned for every user whenever anyone marks or dismisses
notifications, and at startup, the same lazy way as the trash purge.

When each alert fires is worked out in notification_alerts (with the time
math in notification_times and the wording in notification_text); this
module serves them with their read and dismissed state, and holds the
user's preferences.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crud.base import get_user_row
from app.crud.notification_alerts import NOTIFICATION_RETENTION, delivered_alerts
from app.models.notification_model import NotificationPreferences as PreferencesModel
from app.models.notification_model import NotificationState
from app.schemas.notification_schema import (
    NOTIFICATION_TYPES,
    Notification,
    NotificationPreferences,
    notification_occurs_on,
)

DEFAULT_REMINDER_MINUTES = 120
DEFAULT_MOOD_REMINDER_MINUTES = 60


# ── Preferences ──────────────────────────────────────────────────────────────

def _get_preferences_row(db: Session, user_id: str) -> PreferencesModel | None:
    return get_user_row(db, PreferencesModel, user_id)


def _resolve_preferences(row: PreferencesModel | None) -> NotificationPreferences:
    overrides = (row.types if row else None) or {}
    return NotificationPreferences(
        types={t: overrides.get(t, True) for t in NOTIFICATION_TYPES},
        quiet_hours_start=row.quiet_hours_start if row else None,
        quiet_hours_end=row.quiet_hours_end if row else None,
        default_reminder_minutes=(row.default_reminder_minutes if row else None) or DEFAULT_REMINDER_MINUTES,
        mood_reminder_minutes=(row.mood_reminder_minutes if row else None) or DEFAULT_MOOD_REMINDER_MINUTES,
    )


def get_preferences(db: Session, user_id: str) -> NotificationPreferences:
    """The user's notification preferences, with defaults for anything unset: every type on, no quiet hours."""
    return _resolve_preferences(_get_preferences_row(db, user_id))


def update_preferences(db: Session, user_id: str, changes: dict) -> NotificationPreferences:
    """Apply the fields given (from model_dump(exclude_unset=True)). Raises 400 for half-set or empty quiet hours."""
    row = _get_preferences_row(db, user_id) or PreferencesModel(user_id=user_id)
    start = changes.get("quiet_hours_start", row.quiet_hours_start)
    end = changes.get("quiet_hours_end", row.quiet_hours_end)
    if (start is None) != (end is None):
        raise HTTPException(status_code=400, detail="Set both quiet_hours_start and quiet_hours_end, or neither.")
    if start is not None and start == end:
        raise HTTPException(status_code=400, detail="Quiet hours must start and end at different times.")

    if changes.get("types") is not None:
        # A new dict, so the JSON column registers the change.
        types = {t: on for t, on in changes["types"].items() if t in NOTIFICATION_TYPES}
        row.types = {**(row.types or {}), **types}
    row.quiet_hours_start, row.quiet_hours_end = start, end
    for column in ("default_reminder_minutes", "mood_reminder_minutes"):
        if column in changes:
            setattr(row, column, changes[column])
    db.add(row)
    db.commit()
    db.refresh(row)
    return _resolve_preferences(row)


# ── Listing ──────────────────────────────────────────────────────────────────

def get_notifications(db: Session, user_id: str, tz: ZoneInfo, now: datetime) -> list[Notification]:
    """The user's current notifications as of `now`, newest first. Dismissed ones are left out."""
    delivered = delivered_alerts(db, user_id, get_preferences(db, user_id), tz, now)
    states = {
        s.notification_id: s
        for s in db.query(NotificationState).filter(
            NotificationState.user_id == user_id,
            NotificationState.notification_id.in_([alert.id for alert, _ in delivered]),
        )
    }
    notifications = []
    for alert, at in delivered:
        state = states.get(alert.id)
        if state and state.dismissed_at:
            continue
        title, body = alert.render(at.astimezone(tz))
        notifications.append(Notification(
            id=alert.id,
            type=alert.type,
            title=title,
            body=body,
            fire_at=at,
            task_ids=alert.task_ids,
            dates=alert.dates,
            read=bool(state and state.read_at),
        ))
    return notifications


# ── Read and dismissed state ─────────────────────────────────────────────────

def prune_states(db: Session) -> None:
    """Delete every user's state rows for occurrences past NOTIFICATION_RETENTION. Does not commit.

    Occurrence dates are local, so a day of slack covers every time zone.
    """
    cutoff = datetime.now(UTC).date() - NOTIFICATION_RETENTION - timedelta(days=1)
    db.execute(delete(NotificationState).where(NotificationState.occurs_on < cutoff))


def _mark(db: Session, user_id: str, ids: list[str], column: str) -> None:
    """Stamp read_at or dismissed_at on each notification id, keeping an earlier stamp."""
    prune_states(db)
    if ids:
        now = datetime.now(UTC)
        stmt = insert(NotificationState).values([
            {"user_id": user_id, "notification_id": i, "occurs_on": notification_occurs_on(i), column: now}
            for i in dict.fromkeys(ids)
        ])
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "notification_id"],
            set_={column: func.coalesce(getattr(NotificationState, column), stmt.excluded[column])},
        )
        db.execute(stmt)
    db.commit()


def mark_read(db: Session, user_id: str, ids: list[str]) -> None:
    _mark(db, user_id, ids, "read_at")


def mark_all_read(db: Session, user_id: str, tz: ZoneInfo, now: datetime) -> None:
    """Mark every notification the user currently has as read."""
    _mark(db, user_id, [n.id for n in get_notifications(db, user_id, tz, now) if not n.read], "read_at")


def dismiss(db: Session, user_id: str, ids: list[str]) -> None:
    _mark(db, user_id, ids, "dismissed_at")
