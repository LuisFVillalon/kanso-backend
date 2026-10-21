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


def _activity_task(task: TaskModel) -> ActivityTask:
    return ActivityTask.model_validate(task).model_copy(update={"archived": task.archived_at is not None})


def get_calendar_activity(
    db: Session, user_id: str, start: date, end: date, tz: ZoneInfo, include_archived: bool = False
) -> CalendarActivity:
    """Everything the user planned or did across local days start..end inclusive:
    tasks due in the range, tasks completed in it, note editing sessions and
    habit completions. Deleted tasks, notes and habits are left out, and so are
    archived ones unless `include_archived` is set; each item says whether it
    (or its note or habit) is archived.

    Due dates and habit log dates are already local calendar dates. Completion
    and session timestamps are UTC, so they are matched against the UTC
    instants the caller's local range covers in time zone `tz`. Each end is
    resolved on its own, so a range that crosses a DST change stays exact.
    """
    range_start_utc = _local_midnight_utc(start, tz)
    range_end_utc = _local_midnight_utc(end + timedelta(days=1), tz)

    visible_tasks = (
        db.query(TaskModel)
        .options(selectinload(TaskModel.tags))
        .filter(TaskModel.user_id == user_id, TaskModel.deleted_at.is_(None))
    )
    if not include_archived:
        visible_tasks = visible_tasks.filter(TaskModel.archived_at.is_(None))
    scheduled_tasks = (
        visible_tasks.filter(
            TaskModel.due_date >= datetime.combine(start, time.min),
            TaskModel.due_date < datetime.combine(end + timedelta(days=1), time.min),
        )
        .order_by(TaskModel.due_date, TaskModel.due_time.asc().nulls_first(), TaskModel.id)
        .all()
    )
    completed_tasks = (
        visible_tasks.filter(
            TaskModel.completed == True,  # noqa: E712
            TaskModel.completed_date >= range_start_utc,
            TaskModel.completed_date < range_end_utc,
        )
        .order_by(TaskModel.completed_date, TaskModel.id)
        .all()
    )

    session_query = (
        db.query(NoteSessionModel, NoteModel.title, NoteModel.archived_at)
        .join(NoteModel, NoteModel.id == NoteSessionModel.note_id)
        .filter(
            NoteSessionModel.user_id == user_id,
            NoteModel.deleted_at.is_(None),
            # Overlaps the range. Zero-length sessions (reaped orphans, see
            # note_session_crud.reap_stale_sessions) are left out.
            NoteSessionModel.ended_at > range_start_utc,
            NoteSessionModel.started_at < range_end_utc,
            NoteSessionModel.ended_at > NoteSessionModel.started_at,
        )
    )
    if not include_archived:
        session_query = session_query.filter(NoteModel.archived_at.is_(None))
    session_rows = session_query.order_by(NoteSessionModel.started_at, NoteSessionModel.id).all()

    habit_query = (
        db.query(HabitLog.logged_date, Habit.id, Habit.title, Habit.estimated_time, Habit.archived_at)
        .join(Habit, Habit.id == HabitLog.habit_id)
        .filter(
            Habit.user_id == user_id,
            Habit.deleted_at.is_(None),
            HabitLog.logged_date >= start,
            HabitLog.logged_date <= end,
        )
    )
    if not include_archived:
        habit_query = habit_query.filter(Habit.archived_at.is_(None))
    habit_rows = habit_query.order_by(HabitLog.logged_date, func.lower(Habit.title), Habit.id).all()

    return CalendarActivity(
        start_date=start,
        end_date=end,
        scheduled_tasks=[_activity_task(t) for t in scheduled_tasks],
        completed_tasks=[_activity_task(t) for t in completed_tasks],
        note_sessions=[
            ActivityNoteSession(
                id=s.id,
                note_id=s.note_id,
                note_title=title,
                started_at=s.started_at,
                ended_at=s.ended_at,
                duration_seconds=int((s.ended_at - s.started_at).total_seconds()),
                archived=archived_at is not None,
            )
            for s, title, archived_at in session_rows
        ],
        habit_completions=[
            ActivityHabitCompletion(
                habit_id=habit_id,
                habit_title=title,
                date=logged,
                estimated_time=hours,
                archived=archived_at is not None,
            )
            for logged, habit_id, title, hours, archived_at in habit_rows
        ],
    )
