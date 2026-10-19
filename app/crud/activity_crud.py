from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.habit_log_model import HabitLog
from app.models.habit_model import Habit
from app.models.note_model import Note as NoteModel
from app.models.note_session_model import NoteSession as NoteSessionModel
from app.models.task_model import Task as TaskModel
from app.schemas.activity_schema import (
    ActivityHabitCompletion,
    ActivityNoteSession,
    ActivityTask,
    CalendarActivity,
)


def _local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """The naive UTC instant at which the local day `day` starts in `tz`."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def get_calendar_activity(db: Session, user_id: str, start: date, end: date, tz: ZoneInfo) -> CalendarActivity:
    """Everything the user planned or did across local days start..end inclusive:
    tasks due in the range, tasks completed in it, note editing sessions and
    habit completions. Archived and deleted tasks, notes and habits are left out.

    Due dates and habit log dates are already local calendar dates. Completion
    and session timestamps are UTC, so they are matched against the UTC
    instants the caller's local range covers in time zone `tz`. Each end is
    resolved on its own, so a range that crosses a DST change stays exact.
    """
    range_start_utc = _local_midnight_utc(start, tz)
    range_end_utc = _local_midnight_utc(end + timedelta(days=1), tz)

    active_tasks = (
        db.query(TaskModel)
        .options(selectinload(TaskModel.tags))
        .filter(TaskModel.user_id == user_id, TaskModel.archived_at.is_(None), TaskModel.deleted_at.is_(None))
    )
    scheduled_tasks = (
        active_tasks.filter(
            TaskModel.due_date >= datetime.combine(start, time.min),
            TaskModel.due_date < datetime.combine(end + timedelta(days=1), time.min),
        )
        .order_by(TaskModel.due_date, TaskModel.due_time.asc().nulls_first(), TaskModel.id)
        .all()
    )
    completed_tasks = (
        active_tasks.filter(
            TaskModel.completed == True,  # noqa: E712
            TaskModel.completed_date >= range_start_utc,
            TaskModel.completed_date < range_end_utc,
        )
        .order_by(TaskModel.completed_date, TaskModel.id)
        .all()
    )

    session_rows = (
        db.query(NoteSessionModel, NoteModel.title)
        .join(NoteModel, NoteModel.id == NoteSessionModel.note_id)
        .filter(
            NoteSessionModel.user_id == user_id,
            NoteModel.archived_at.is_(None),
            NoteModel.deleted_at.is_(None),
            # Overlaps the range. Zero-length sessions (reaped orphans, see
            # note_session_crud.reap_stale_sessions) are left out.
            NoteSessionModel.ended_at > range_start_utc,
            NoteSessionModel.started_at < range_end_utc,
            NoteSessionModel.ended_at > NoteSessionModel.started_at,
        )
        .order_by(NoteSessionModel.started_at, NoteSessionModel.id)
        .all()
    )

    habit_rows = (
        db.query(HabitLog.logged_date, Habit.id, Habit.title, Habit.estimated_time)
        .join(Habit, Habit.id == HabitLog.habit_id)
        .filter(
            Habit.user_id == user_id,
            Habit.archived_at.is_(None),
            Habit.deleted_at.is_(None),
            HabitLog.logged_date >= start,
            HabitLog.logged_date <= end,
        )
        .order_by(HabitLog.logged_date, func.lower(Habit.title), Habit.id)
        .all()
    )

    return CalendarActivity(
        start_date=start,
        end_date=end,
        scheduled_tasks=[ActivityTask.model_validate(t) for t in scheduled_tasks],
        completed_tasks=[ActivityTask.model_validate(t) for t in completed_tasks],
        note_sessions=[
            ActivityNoteSession(
                id=s.id,
                note_id=s.note_id,
                note_title=title,
                started_at=s.started_at,
                ended_at=s.ended_at,
                duration_seconds=int((s.ended_at - s.started_at).total_seconds()),
            )
            for s, title in session_rows
        ],
        habit_completions=[
            ActivityHabitCompletion(habit_id=habit_id, habit_title=title, date=logged, estimated_time=hours)
            for logged, habit_id, title, hours in habit_rows
        ],
    )
