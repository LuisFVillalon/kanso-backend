from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.crud.tag_crud import get_or_create_tag
from app.models.task_model import Task as TaskModel
from app.schemas.task_schema import TaskCreate


def get_tasks(db: Session, user_id: str):
    """Return all tasks owned by the given user."""
    return db.query(TaskModel).filter(TaskModel.user_id == user_id).all()


def _validate_priority_unique(db: Session, user_id: str, priority: int, exclude_task_id: int = None) -> None:
    """Raise 400 if another task for this user already has the same priority."""
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.priority == priority,
    )
    if exclude_task_id is not None:
        q = q.filter(TaskModel.id != exclude_task_id)
    if q.first():
        raise HTTPException(
            status_code=400,
            detail=f"Priority {priority} is already assigned to another task.",
        )


def _validate_parent_task_id(db: Session, parent_task_id: int, user_id: str) -> None:
    """Raise 400 if the parent task doesn't exist or belongs to a different user."""
    if parent_task_id is None:
        return
    parent_task = (
        db.query(TaskModel)
        .filter(TaskModel.id == parent_task_id, TaskModel.user_id == user_id)
        .first()
    )
    if not parent_task:
        raise HTTPException(
            status_code=400,
            detail=f"Parent task with ID {parent_task_id} not found",
        )


def create_task(db: Session, task: TaskCreate, user_id: str):
    """Create and persist a new task, auto-creating any new tags."""
    now = datetime.now(UTC)
    priority = task.priority
    if priority is not None:
        _validate_priority_unique(db, user_id, priority)
    _validate_parent_task_id(db, task.parent_task_id, user_id)

    db_task = TaskModel(
        title=task.title,
        description=task.description,
        category=task.category,
        completed=task.completed,
        priority=priority,
        due_date=task.due_date,
        due_time=task.due_time,
        created_date=now,
        completed_date=now if task.completed else None,
        estimated_time=task.estimated_time,
        parent_task_id=task.parent_task_id,
        user_id=user_id,
    )
    for tag_data in task.tags:
        db_task.tags.append(get_or_create_tag(db, tag_data, user_id))
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


def delete_task(db: Session, task_id: int, user_id: str):
    """Delete a task. Returns the deleted record, or None if not found."""
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    db.delete(task)
    db.commit()
    return task


def update_task_status(db: Session, task_id: int, user_id: str):
    """Toggle a task's completed status and update completed_date accordingly."""
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    task.completed = not task.completed
    task.completed_date = datetime.now(UTC) if task.completed else None
    db.commit()
    db.refresh(task)
    return task


_TASK_UPDATE_FIELDS = (
    "title", "description", "category", "completed", "due_date",
    "due_time", "estimated_time", "parent_task_id",
)


def update_task(db: Session, task_id: int, task: TaskCreate, user_id: str):
    """Replace all fields of an existing task. Returns None if not found."""
    db_task = get_owned(db, TaskModel, task_id, user_id)
    if not db_task:
        return None

    if task.parent_task_id is not None:
        if task.parent_task_id == task_id:
            raise HTTPException(status_code=400, detail="A task cannot be its own parent")
        _validate_parent_task_id(db, task.parent_task_id, user_id)

    if task.priority is not None:
        _validate_priority_unique(db, user_id, task.priority, exclude_task_id=task_id)
    db_task.priority = task.priority

    was_completed = db_task.completed

    for field in _TASK_UPDATE_FIELDS:
        setattr(db_task, field, getattr(task, field))

    # completed_date is always server-computed (like create_task/update_task_status)
    # rather than trusting the client's value: the client sends its own local
    # wall-clock time, not a true UTC timestamp, and the debrief's "completed
    # today" window math (see debrief_crud._local_day_bounds_utc) assumes
    # completed_date is genuine UTC — trusting the client value made that
    # comparison misalign by the caller's UTC offset, silently dropping
    # same-day completions out of the daily brief's activity log.
    if task.completed and not was_completed:
        db_task.completed_date = datetime.now(UTC)
    elif not task.completed:
        db_task.completed_date = None

    db_task.tags.clear()
    for tag_data in task.tags:
        db_task.tags.append(get_or_create_tag(db, tag_data, user_id))
    db.flush()

    db.commit()
    db.refresh(db_task)
    return db_task
