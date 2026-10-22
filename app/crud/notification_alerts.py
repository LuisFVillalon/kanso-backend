"""
Working out which alerts have fired, from the user's tasks, mood check-ins
and profile. See notification_crud for the alert types.

Each builder returns every alert that could apply around today, with when
it fires and when it lapses; delivered_alerts() then keeps the ones the
user has switched on that have fired by now, held through quiet hours, and
not yet lapsed.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.notification_text import clock, count_of, duration, list_days, relative_when
from app.crud.notification_times import (
    DEFAULT_DAY_START,
    DEFAULT_SHUTOFF,
    hold_for_quiet_hours,
    local_instant,
    local_midnight_after,
    parse_hhmm,
    shutoff_at,
)
from app.crud.profile_crud import get_profile, is_rest_day
from app.crud.task_rules import open_task_filter
from app.models.mood_checkin_model import MoodCheckin
from app.models.task_model import Task as TaskModel
from app.schemas.limits import REMINDER_MINUTES_MAX
from app.schemas.mood_schema import resolve_mood_settings
from app.schemas.notification_schema import NotificationPreferences

NOTIFICATION_RETENTION = timedelta(days=30)
FINAL_WARNING = timedelta(minutes=30)
# How many days back mood_missed looks for missing check-ins.
MOOD_MISSED_DAYS = 7


@dataclass
class Alert:
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


@dataclass(frozen=True)
class _UserDay:
    """The user's local day, as their profile shapes it."""

    today: date
    tz: ZoneInfo
    day_start: time
    shutoff: time
    rest_days: list[int]


def _task_alerts(tasks: list[TaskModel], default_lead: int, enabled: dict, tz: ZoneInfo) -> list[Alert]:
    alerts: list[Alert] = []
    for task in tasks:
        due_day = task.due_date.date()
        subject = f"task-{task.id}"
        due_at = local_instant(due_day, task.due_time, tz)
        occurrence = f"{due_day.isoformat()}T{task.due_time:%H:%M}"
        due_local = due_at.astimezone(tz)

        def countdown(delivered: datetime, t=task, due_at=due_at, due_local=due_local) -> tuple[str, str]:
            return f"{t.title} is due in {duration(due_at - delivered)}", f"Due {relative_when(due_local, delivered)}."

        warning_at = due_at - FINAL_WARNING
        lead = timedelta(minutes=task.reminder_minutes or default_lead)
        # A lead no longer than the final warning's would only repeat it.
        if lead > FINAL_WARNING:
            alerts.append(Alert(
                "task_reminder", subject, occurrence,
                fire_at=due_at - lead,
                # The final warning takes over, if the user gets it.
                expires_at=warning_at if enabled["final_warning"] else due_at,
                render=countdown,
                task_ids=[task.id],
            ))
        alerts.append(Alert(
            "final_warning", subject, occurrence,
            fire_at=warning_at,
            expires_at=due_at,
            render=countdown,
            task_ids=[task.id],
        ))
    return alerts


def _mood_alerts(db: Session, user_id: str, day: _UserDay, lead: int, enabled: dict) -> list[Alert]:
    today, rest_days = day.today, day.rest_days
    daily = db.query(MoodCheckin.period_start).filter(
        MoodCheckin.user_id == user_id, MoodCheckin.period_kind == "day"
    )
    logged = {
        d for (d,) in daily.filter(MoodCheckin.period_start >= today - timedelta(days=MOOD_MISSED_DAYS))
    }

    alerts = []
    if enabled["mood_reminder"]:
        # A "Call it a day" at or before the day start falls after midnight,
        # so yesterday's reminder can still be live early today.
        for d in (today - timedelta(days=1), today):
            if is_rest_day(d, rest_days) or d in logged:
                continue
            called_at = shutoff_at(d, day.shutoff, day.day_start, day.tz)
            alerts.append(Alert(
                "mood_reminder", "mood", d.isoformat(),
                fire_at=max(called_at - timedelta(minutes=lead), local_instant(d, day.day_start, day.tz)),
                expires_at=max(local_midnight_after(d, day.tz), called_at),
                render=lambda _, shutoff=day.shutoff: (
                    "How was your day?",
                    f"Log your mood check-in before you call it a day at {clock(shutoff)}.",
                ),
                dates=[d],
            ))

    if enabled["mood_missed"] and not is_rest_day(today, rest_days):
        # When daily check-ins were switched on isn't stored, so days count
        # from the first one the user logged.
        first = daily.with_entities(func.min(MoodCheckin.period_start)).scalar()
        missed = [
            d for d in (today - timedelta(days=n) for n in range(MOOD_MISSED_DAYS, 0, -1))
            if first and first <= d and d not in logged and not is_rest_day(d, rest_days)
        ]
        if missed:
            alerts.append(Alert(
                "mood_missed", "mood", today.isoformat(),
                fire_at=local_instant(today, day.day_start, day.tz),
                expires_at=local_midnight_after(today, day.tz),
                render=lambda _, missed=missed: (
                    count_of(len(missed), "missed mood check-in"),
                    f"No check-in for {list_days(missed)} yet.",
                ),
                dates=missed,
            ))
    return alerts


def delivered_alerts(
    db: Session, user_id: str, prefs: NotificationPreferences, tz: ZoneInfo, now: datetime
) -> list[tuple[Alert, datetime]]:
    """Every alert delivered by `now` and still applying, with its delivery time, newest first."""
    enabled = prefs.types
    profile = get_profile(db, user_id)
    day = _UserDay(
        today=now.astimezone(tz).date(),
        tz=tz,
        day_start=parse_hhmm(profile.day_start_time if profile else None, DEFAULT_DAY_START),
        shutoff=parse_hhmm(profile.shutoff_time if profile else None, DEFAULT_SHUTOFF),
        rest_days=(profile.rest_days if profile else None) or [],
    )
    quiet_start, quiet_end = parse_hhmm(prefs.quiet_hours_start), parse_hhmm(prefs.quiet_hours_end)
    quiet = (quiet_start, quiet_end) if quiet_start and quiet_end and quiet_start != quiet_end else None

    today = day.today
    oldest = today - NOTIFICATION_RETENTION
    tasks = (
        db.query(TaskModel)
        .filter(
            TaskModel.user_id == user_id,
            open_task_filter(),
            TaskModel.due_time.is_not(None),
            TaskModel.due_date >= datetime.combine(oldest, time.min),
            # The longest reminder lead reaches this far ahead.
            TaskModel.due_date < datetime.combine(today + timedelta(minutes=REMINDER_MINUTES_MAX, days=2), time.min),
        )
        .order_by(TaskModel.due_date, TaskModel.due_time, TaskModel.id)
        .all()
    )
    alerts = _task_alerts(tasks, prefs.default_reminder_minutes, enabled, tz)
    if enabled["mood_reminder"] or enabled["mood_missed"]:
        settings = resolve_mood_settings(profile.mood_checkins if profile else None)
        if settings.daily:
            alerts += _mood_alerts(db, user_id, day, prefs.mood_reminder_minutes, enabled)

    delivered = []
    for alert in alerts:
        if not enabled[alert.type]:
            continue
        at = hold_for_quiet_hours(alert.fire_at, quiet, tz)
        if at <= now and (alert.expires_at is None or now < alert.expires_at):
            delivered.append((alert, at))
    return sorted(delivered, key=lambda pair: (pair[1], pair[0].id), reverse=True)
