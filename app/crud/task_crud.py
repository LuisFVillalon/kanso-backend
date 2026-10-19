from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.crud.base import archived_filter, get_owned
from app.crud.tag_crud import set_item_tags
from app.models.task_model import Task as TaskModel
from app.schemas.task_schema import TaskCreate


def get_tasks(db: Session, user_id: str, archived: bool = False):
    """Return the user's active tasks, or only their archived ones when archived=True."""
    return db.query(TaskModel).filter(TaskModel.user_id == user_id, archived_filter(TaskModel, archived)).all()


def _priority_taken(db: Session, user_id: str, priority: int, exclude_task_id: int = None) -> bool:
    """True if another active task for this user already has the same priority.

    Archived tasks keep their priority but don't hold it: they're hidden from
    the user, who would otherwise be blocked by a task they can't see.
    """
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.priority == priority,
        TaskModel.archived_at.is_(None),
    )
    if exclude_task_id is not None:
        q = q.filter(TaskModel.id != exclude_task_id)
    return q.first() is not None


def _validate_priority_unique(db: Session, user_id: str, priority: int, exclude_task_id: int = None) -> None:
    """Raise 400 if another active task for this user already has the same priority."""
    if _priority_taken(db, user_id, priority, exclude_task_id):
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
    set_item_tags(db, db_task, task.tags, user_id)
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

    set_item_tags(db, db_task, task.tags, user_id)
    db.flush()

    db.commit()
    db.refresh(db_task)
    return db_task


def _descendants(db: Session, task: TaskModel, user_id: str) -> list[TaskModel]:
    """Every subtask below a task, at any depth."""
    found: list[TaskModel] = []
    seen = {task.id}
    frontier = [task.id]
    while frontier:
        children = (
            db.query(TaskModel)
            .filter(TaskModel.parent_task_id.in_(frontier), TaskModel.user_id == user_id)
            .all()
        )
        children = [c for c in children if c.id not in seen]
        seen.update(c.id for c in children)
        found.extend(children)
        frontier = [c.id for c in children]
    return found


def _archived_ancestors(db: Session, task: TaskModel, user_id: str) -> list[TaskModel]:
    """The unbroken chain of archived parents directly above a task."""
    found: list[TaskModel] = []
    seen = {task.id}
    parent_id = task.parent_task_id
    while parent_id is not None and parent_id not in seen:
        parent = get_owned(db, TaskModel, parent_id, user_id)
        if not parent or parent.archived_at is None:
            break
        seen.add(parent.id)
        found.append(parent)
        parent_id = parent.parent_task_id
    return found


def archive_task(db: Session, task_id: int, user_id: str):
    """Archive a task and its active subtasks. Returns None if not found.

    The subtasks get the parent's archived_at, which is how unarchive_task
    tells them apart from subtasks archived on their own. Idempotent: an
    already-archived task keeps its original archived_at.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = task.archived_at or datetime.now(UTC)
    for t in [task, *_descendants(db, task, user_id)]:
        if t.archived_at is None:
            t.archived_at = stamp
    db.commit()
    db.refresh(task)
    return task


def unarchive_task(db: Session, task_id: int, user_id: str):
    """Restore a task and the subtasks archived along with it. Returns None if not found.

    Subtasks archived separately, before the parent, stay archived. Archived
    ancestors are restored too (not their other subtasks), so the task never
    comes back under a hidden parent. A restored
    task whose priority was taken by another task while it was archived comes
    back without a priority rather than duplicating it.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    if task.archived_at is not None:
        stamp = task.archived_at
        restored = [
            *_archived_ancestors(db, task, user_id),
            task,
            *[t for t in _descendants(db, task, user_id) if t.archived_at == stamp],
        ]
        for t in restored:
            t.archived_at = None
            db.flush()
            if t.priority is not None and _priority_taken(db, user_id, t.priority, exclude_task_id=t.id):
                t.priority = None
        db.commit()
        db.refresh(task)
    return task
