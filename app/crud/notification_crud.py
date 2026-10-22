"""
In-app notifications, computed on demand.

Nothing is scheduled or queued: each GET works out, from the user's tasks,
mood check-ins, profile and notification preferences, which alerts have
fired by now in the caller's time zone. The alerts are:

- task_reminder: a task's reminder_minutes (or the user's default) before
  its due time, until it is due.
- mood_reminder: mood_reminder_minutes (default 60) before "Call it a day",
  but not before the day start, while the day's mood check-in isn't logged.
  Not on rest days.
- mood_missed: at the day start, listing the past week's days with no mood
  check-in, counted from the user's first one. Not on rest days.

The mood_reminder switch turns daily check-ins on and off, so both mood
alerts follow it, and mood_missed also has its own switch. They only say
that a check-in is due or missing, never what one holds.

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
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.mood_checkin_model import MoodCheckin
from app.models.notification_model import NotificationPreferences as PreferencesModel
from app.models.notification_model import NotificationState
from app.models.profile_model import Profile
from app.models.task_model import Task as TaskModel
from app.schemas.limits import REMINDER_MINUTES_MAX
from app.schemas.notification_schema import (
    NOTIFICATION_TYPES,
    Notification,
    NotificationPreferences,
    notification_occurs_on,
)

NOTIFICATION_RETENTION = timedelta(days=30)
DEFAULT_REMINDER_MINUTES = 120
DEFAULT_MOOD_REMINDER_MINUTES = 60
# How many days back mood_missed looks for missing check-ins.
MOOD_MISSED_DAYS = 7
# The frontend's defaults for a profile without these times set.
DEFAULT_DAY_START = time(8, 0)
DEFAULT_SHUTOFF = time(22, 0)


# ── Preferences ──────────────────────────────────────────────────────────────

def _get_preferences_row(db: Session, user_id: str) -> PreferencesModel | None:
    return db.query(PreferencesModel).filter(PreferencesModel.user_id == user_id).first()


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


# ── Time helpers ─────────────────────────────────────────────────────────────

def _parse_hhmm(value: str | None, default: time | None = None) -> time | None:
    """Parse a stored "HH:MM" local time, falling back to default if missing or malformed."""
    if not value:
        return default
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except ValueError:
        return default


def _at(day: date, t: time, tz: ZoneInfo, fold: int = 0) -> datetime:
    """The UTC instant of local wall time `t` on `day` in `tz`.

    A time skipped by a DST change resolves to the matching instant after it
    (02:30 on a spring-forward night is 03:30); a repeated one to its first
    pass unless fold=1.
    """
    return datetime.combine(day, t, tzinfo=tz).replace(fold=fold).astimezone(UTC)


def _hold_for_quiet_hours(fire_at: datetime, quiet: tuple[time, time] | None, tz: ZoneInfo) -> datetime:
    """When an alert firing at fire_at is delivered: at once, or when the quiet hours around it end.

    Quiet hours with start after end run past midnight (22:00 to 07:00).
    """
    if quiet is None:
        return fire_at
    start, end = quiet
    local = fire_at.astimezone(tz)
    wall, day = local.time(), local.date()
    if start < end:
        if not start <= wall < end:
            return fire_at
    elif wall >= start:
        day += timedelta(days=1)
    elif wall >= end:
        return fire_at
    # On a fall-back night the end's first pass can already be behind fire_at.
    for fold in (0, 1):
        held = _at(day, end, tz, fold)
        if held > fire_at:
            return held
    return fire_at


def _shutoff_at(day: date, shutoff: time, day_start: time, tz: ZoneInfo) -> datetime:
    """When `day` is called a day. A "Call it a day" at or before the day start falls after midnight."""
    return _at(day + timedelta(days=1) if shutoff <= day_start else day, shutoff, tz)


def _is_rest_day(day: date, rest_days: list[int]) -> bool:
    return day.isoweekday() % 7 in rest_days  # 0=Sun..6=Sat, matches Profile.rest_days


def _clock(t: time) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _day_label(day: date) -> str:
    return f"{day:%a, %b} {day.day}"


def _when(due_local: datetime, from_local: datetime) -> str:
    """"today at 4:00 PM", "tomorrow at 9:00 AM" or "on Wed, Oct 21 at 9:00 AM", as seen from from_local."""
    clock = _clock(due_local.time())
    days = (due_local.date() - from_local.date()).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"on {_day_label(due_local.date())} at {clock}"


def _duration(delta: timedelta) -> str:
    """A countdown such as "2 hours", "1 hour 30 minutes" or "1 day", rounded to the minute."""
    minutes = max(1, round(delta.total_seconds() / 60))
    parts = []
    for size, unit in ((24 * 60, "day"), (60, "hour"), (1, "minute")):
        count, minutes = divmod(minutes, size)
        if count:
            parts.append(f"{count} {unit}{'' if count == 1 else 's'}")
    return " ".join(parts[:2])


def _list_days(days: list[date]) -> str:
    """"Oct 19", "Oct 19 and Oct 20" or "Oct 18, Oct 19 and Oct 20"."""
    labels = [f"{day:%b} {day.day}" for day in days]
    return " and ".join(filter(None, (", ".join(labels[:-1]), labels[-1])))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


# ── Computing notifications ──────────────────────────────────────────────────

@dataclass
class _Alert:
    type: str
    subject: str
    occurrence: str
    fire_at: datetime
    # The alert is dropped if not delivered before this; None means it never lapses.
    expires_at: datetime | None
    # Builds (title, body) from the local delivery time.
    render: Callable[[datetime], tuple[str, str]]
    task_ids: list[int] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.type}:{self.subject}:{self.occurrence}"


def _task_alerts(tasks: list[TaskModel], default_lead: int, tz: ZoneInfo) -> list[_Alert]:
    alerts: list[_Alert] = []
    for task in tasks:
        due_day = task.due_date.date()
        subject = f"task-{task.id}"
        due_at = _at(due_day, task.due_time, tz)
        occurrence = f"{due_day.isoformat()}T{task.due_time:%H:%M}"
        due_local = due_at.astimezone(tz)

        def countdown(delivered: datetime, t=task, due_at=due_at, due_local=due_local) -> tuple[str, str]:
            return f"{t.title} is due in {_duration(due_at - delivered)}", f"Due {_when(due_local, delivered)}."

        lead = timedelta(minutes=task.reminder_minutes or default_lead)
        alerts.append(_Alert(
            "task_reminder", subject, occurrence,
            fire_at=due_at - lead,
            expires_at=due_at,
            render=countdown,
            task_ids=[task.id],
        ))
    return alerts


def _mood_alerts(
    db: Session, user_id: str, today: date, day_start: time, profile: Profile | None, lead: int,
    enabled: dict, tz: ZoneInfo,
) -> list[_Alert]:
    shutoff = _parse_hhmm(profile.shutoff_time if profile else None, DEFAULT_SHUTOFF)
    rest_days = (profile.rest_days if profile else None) or []
    daily = db.query(MoodCheckin.period_start).filter(
        MoodCheckin.user_id == user_id, MoodCheckin.period_kind == "day"
    )
    logged = {
        day for (day,) in daily.filter(MoodCheckin.period_start >= today - timedelta(days=MOOD_MISSED_DAYS))
    }

    alerts = []
    # A "Call it a day" at or before the day start falls after midnight,
    # so yesterday's reminder can still be live early today.
    for day in (today - timedelta(days=1), today):
        if _is_rest_day(day, rest_days) or day in logged:
            continue
        shutoff_at = _shutoff_at(day, shutoff, day_start, tz)
        alerts.append(_Alert(
            "mood_reminder", "mood", day.isoformat(),
            fire_at=max(shutoff_at - timedelta(minutes=lead), _at(day, day_start, tz)),
            expires_at=max(_at(day + timedelta(days=1), time.min, tz), shutoff_at),
            render=lambda _, shutoff=shutoff: (
                "How was your day?",
                f"Log your mood check-in before you call it a day at {_clock(shutoff)}.",
            ),
            dates=[day],
        ))

    if enabled["mood_missed"] and not _is_rest_day(today, rest_days):
        # When daily check-ins were switched on isn't stored, so days count
        # from the first one the user logged.
        first = daily.with_entities(func.min(MoodCheckin.period_start)).scalar()
        missed = [
            day for day in (today - timedelta(days=n) for n in range(MOOD_MISSED_DAYS, 0, -1))
            if first and first <= day and day not in logged and not _is_rest_day(day, rest_days)
        ]
        if missed:
            alerts.append(_Alert(
                "mood_missed", "mood", today.isoformat(),
                fire_at=_at(today, day_start, tz),
                expires_at=_at(today + timedelta(days=1), time.min, tz),
                render=lambda _, missed=missed: (
                    _count(len(missed), "missed mood check-in"),
                    f"No check-in for {_list_days(missed)} yet.",
                ),
                dates=missed,
            ))
    return alerts


def _compute(db: Session, user_id: str, tz: ZoneInfo, now: datetime) -> list[tuple[_Alert, datetime]]:
    """Every alert delivered by `now` and still applying, with its delivery time, newest first."""
    prefs = get_preferences(db, user_id)
    enabled = prefs.types
    profile = db.query(Profile).filter(Profile.user_id == user_id).first()
    day_start = _parse_hhmm(profile.day_start_time if profile else None, DEFAULT_DAY_START)
    quiet_start, quiet_end = _parse_hhmm(prefs.quiet_hours_start), _parse_hhmm(prefs.quiet_hours_end)
    quiet = (quiet_start, quiet_end) if quiet_start and quiet_end and quiet_start != quiet_end else None

    today = now.astimezone(tz).date()
    oldest = today - NOTIFICATION_RETENTION
    tasks = (
        db.query(TaskModel)
        .filter(
            TaskModel.user_id == user_id,
            TaskModel.archived_at.is_(None),
            TaskModel.deleted_at.is_(None),
            TaskModel.completed.is_not(True),
            TaskModel.due_time.is_not(None),
            TaskModel.due_date >= datetime.combine(oldest, time.min),
            # The longest reminder lead reaches this far ahead.
            TaskModel.due_date < datetime.combine(today + timedelta(minutes=REMINDER_MINUTES_MAX, days=2), time.min),
        )
        .order_by(TaskModel.due_date, TaskModel.due_time, TaskModel.id)
        .all()
    )
    alerts = _task_alerts(tasks, prefs.default_reminder_minutes, tz)
    # The mood reminder's switch turns daily check-ins on and off as a whole.
    if enabled["mood_reminder"]:
        alerts += _mood_alerts(db, user_id, today, day_start, profile, prefs.mood_reminder_minutes, enabled, tz)

    delivered = []
    for alert in alerts:
        if not enabled[alert.type]:
            continue
        at = _hold_for_quiet_hours(alert.fire_at, quiet, tz)
        if at <= now and (alert.expires_at is None or now < alert.expires_at):
            delivered.append((alert, at))
    return sorted(delivered, key=lambda pair: (pair[1], pair[0].id), reverse=True)


def get_notifications(db: Session, user_id: str, tz: ZoneInfo, now: datetime) -> list[Notification]:
    """The user's current notifications as of `now`, newest first. Dismissed ones are left out."""
    delivered = _compute(db, user_id, tz, now)
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
