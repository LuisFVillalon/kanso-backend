"""
Work blocks: the "work on it" times a user plans on the calendar for a task
or a habit. The frontend picks the times; the backend only stores them.

They are kept apart from events, so Reports and event reminders never count
them. A block shows while its task or habit does: one whose task or habit is
in the trash is hidden (and back on restore), and one whose task or habit is
archived shows only where archived items are asked for. Completing or
archiving a task deletes its blocks that haven't started yet and keeps the
rest as history; deleting a task or habit for good deletes all of its blocks
(the database cascades).
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from app.crud.base import get_owned
from app.models.habit_model import Habit
from app.models.task_model import Task
from app.models.work_block_model import WorkBlock
from app.schemas.work_block_schema import WorkBlock as WorkBlockOut
from app.schemas.work_block_schema import WorkBlockCreate, WorkBlockUpdate


def _span(day: date, start_time: time, end_time: time, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC instants of a local span; an end before the start is on the next day.

    As for events, a time skipped by a DST change resolves to the matching
    instant after it (02:30 is 03:30).
    """
    end_day = day + timedelta(days=1) if end_time < start_time else day
    starts_at = datetime.combine(day, start_time, tzinfo=tz).astimezone(UTC)
    ends_at = datetime.combine(end_day, end_time, tzinfo=tz).astimezone(UTC)
    return starts_at, ends_at


def _place(block: WorkBlock, day: date, start_time: time, end_time: time, time_zone: str) -> None:
    block.date, block.start_time, block.end_time, block.time_zone = day, start_time, end_time, time_zone
    block.starts_at, block.ends_at = _span(day, start_time, end_time, ZoneInfo(time_zone))


def present(block: WorkBlock, tz: ZoneInfo | None = None) -> WorkBlockOut:
    """A block as placed in `tz` (its own time zone if None), with its task's or habit's title and tags."""
    owner = block.task if block.task_id is not None else block.habit
    if tz is None:
        day, start_time, end_time = block.date, block.start_time, block.end_time
    else:
        local_start, local_end = block.starts_at.astimezone(tz), block.ends_at.astimezone(tz)
        day, start_time, end_time = local_start.date(), local_start.time(), local_end.time()
    return WorkBlockOut(
        id=block.id,
        task_id=block.task_id,
        habit_id=block.habit_id,
        title=owner.title,
        tags=owner.tags,
        date=day,
        start_time=start_time,
        end_time=end_time,
        time_zone=block.time_zone,
        starts_at=block.starts_at.astimezone(UTC),
        ends_at=block.ends_at.astimezone(UTC),
    )


def _check_owner(db: Session, user_id: str, task_id: int | None, habit_id: int | None) -> None:
    """404 unless the task or habit is the user's and not in the trash; 400 if it can't take new blocks."""
    if task_id is not None:
        task = get_owned(db, Task, task_id, user_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        if task.archived_at is not None or task.completed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Only an open, active task can be planned"
            )
    else:
        habit = get_owned(db, Habit, habit_id, user_id)
        if habit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Habit not found")
        if habit.archived_at is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only an active habit can be planned")


def create_work_block(db: Session, payload: WorkBlockCreate, user_id: str) -> WorkBlockOut:
    _check_owner(db, user_id, payload.task_id, payload.habit_id)
    block = WorkBlock(user_id=user_id, task_id=payload.task_id, habit_id=payload.habit_id)
    _place(block, payload.date, payload.start_time, payload.end_time, payload.time_zone)
    db.add(block)
    db.commit()
    db.refresh(block)
    return present(block)


def _get_visible(db: Session, block_id: int, user_id: str) -> WorkBlock | None:
    """The user's block, unless its task or habit is in the trash (then it's hidden, as if gone)."""
    block = get_owned(db, WorkBlock, block_id, user_id)
    if block is None:
        return None
    owner = block.task if block.task_id is not None else block.habit
    return None if owner.deleted_at is not None else block


def update_work_block(db: Session, block_id: int, payload: WorkBlockUpdate, user_id: str) -> WorkBlockOut | None:
    """Move or resize a block. Returns None if not found."""
    block = _get_visible(db, block_id, user_id)
    if block is None:
        return None
    _place(block, payload.date, payload.start_time, payload.end_time, block.time_zone)
    db.commit()
    db.refresh(block)
    return present(block)


def delete_work_block(db: Session, block_id: int, user_id: str) -> WorkBlockOut | None:
    """Delete a block. Returns what it held, or None if not found."""
    block = _get_visible(db, block_id, user_id)
    if block is None:
        return None
    # Read it out first: a deleted row can't be loaded again after the commit.
    deleted = present(block)
    db.delete(block)
    db.commit()
    return deleted


def blocks_between(
    db: Session,
    user_id: str,
    span_start: datetime,
    span_end: datetime,
    tz: ZoneInfo | None,
    include_archived: bool = False,
) -> list[WorkBlockOut]:
    """The user's visible blocks overlapping the UTC span [span_start, span_end), in start order, placed in `tz`."""
    query = (
        db.query(WorkBlock)
        .outerjoin(Task, Task.id == WorkBlock.task_id)
        .outerjoin(Habit, Habit.id == WorkBlock.habit_id)
        .options(
            selectinload(WorkBlock.task).selectinload(Task.tags),
            selectinload(WorkBlock.habit).selectinload(Habit.tags),
        )
        .filter(
            WorkBlock.user_id == user_id,
            WorkBlock.ends_at > span_start,
            WorkBlock.starts_at < span_end,
            or_(
                and_(WorkBlock.task_id.isnot(None), Task.deleted_at.is_(None)),
                and_(WorkBlock.habit_id.isnot(None), Habit.deleted_at.is_(None)),
            ),
        )
    )
    if not include_archived:
        query = query.filter(Task.archived_at.is_(None), Habit.archived_at.is_(None))
    return [present(b, tz) for b in query.order_by(WorkBlock.starts_at, WorkBlock.id).all()]


def drop_future_blocks(db: Session, tasks: Iterable[Task], now: datetime | None = None) -> None:
    """Delete the blocks of these tasks that haven't started by `now`, for a task just completed or archived.

    Blocks that started already are kept as history. Flushes but does not commit.
    """
    task_ids = [t.id for t in tasks]
    if not task_ids:
        return
    db.query(WorkBlock).filter(
        WorkBlock.task_id.in_(task_ids), WorkBlock.starts_at > (now or datetime.now(UTC))
    ).delete(synchronize_session=False)
    db.flush()
