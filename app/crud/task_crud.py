from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import archived_filter, deleted_filter, get_owned
from app.crud.tag_crud import set_item_tags
from app.crud.trash_crud import get_trashed, purge_expired
from app.models.task_model import Task as TaskModel
from app.schemas.task_schema import LinkedTaskRef, TaskCreate


def get_tasks(db: Session, user_id: str, archived: bool = False):
    """Return the user's active tasks, or only their archived ones when archived=True. Deleted tasks are left out."""
    return (
        db.query(TaskModel)
        .filter(TaskModel.user_id == user_id, deleted_filter(TaskModel, False), archived_filter(TaskModel, archived))
        .all()
    )


def _priority_taken(db: Session, user_id: str, priority: int, exclude_task_id: int = None) -> bool:
    """True if another active task for this user already has the same priority.

    Archived and deleted tasks keep their priority but don't hold it: they're
    hidden from the user, who would otherwise be blocked by a task they can't see.
    """
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.priority == priority,
        TaskModel.archived_at.is_(None),
        TaskModel.deleted_at.is_(None),
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


def _get_parent(db: Session, parent_task_id: int, user_id: str) -> TaskModel:
    """The user's live task to link under, or 400 if it doesn't exist or belongs to a different user."""
    parent = get_owned(db, TaskModel, parent_task_id, user_id)
    if not parent:
        raise HTTPException(
            status_code=400,
            detail=f"Parent task with ID {parent_task_id} not found",
        )
    return parent


def _ancestor_ids(db: Session, task: TaskModel, user_id: str) -> set[int]:
    """Ids of every task above this one, following parent links until they end or repeat."""
    found: set[int] = set()
    parent_id = task.parent_task_id
    while parent_id is not None and parent_id not in found and parent_id != task.id:
        found.add(parent_id)
        parent_id = (
            db.query(TaskModel.parent_task_id)
            .filter(TaskModel.id == parent_id, TaskModel.user_id == user_id)
            .scalar()
        )
    return found


def _has_linked_tasks(db: Session, task_id: int, user_id: str) -> bool:
    """True if any undeleted task, archived ones included, is linked under this one."""
    return (
        db.query(TaskModel.id)
        .filter(TaskModel.parent_task_id == task_id, TaskModel.user_id == user_id, deleted_filter(TaskModel, False))
        .first()
        is not None
    )


def _validate_link(db: Session, parent: TaskModel, child_id: int | None, user_id: str) -> None:
    """Raise 400 unless a task (child_id, or None for a new one) can be linked under parent.

    Linked tasks go one level deep: a task linked under another can't have
    linked tasks of its own, and a task that has them can't be linked under
    another. That alone rules out loops for links saved through the API; the
    explicit loop check also covers deeper trees saved before the rule.
    Archived tasks can't take new linked tasks, since they're hidden from
    the lists the links would show in.
    """
    if child_id is not None:
        if child_id == parent.id:
            raise HTTPException(status_code=400, detail="A task cannot be its own parent")
        if child_id in _ancestor_ids(db, parent, user_id):
            raise HTTPException(
                status_code=400, detail=f"Linking task {child_id} under task {parent.id} would create a loop"
            )
    if parent.archived_at is not None:
        raise HTTPException(
            status_code=400, detail=f"Task {parent.id} is archived; restore it before linking tasks to it"
        )
    if parent.parent_task_id is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Task {parent.id} is itself a linked task, and linked tasks can't have their own",
        )
    if child_id is not None and _has_linked_tasks(db, child_id, user_id):
        raise HTTPException(
            status_code=400,
            detail=f"Task {child_id} has linked tasks of its own, so it can't be linked under another task",
        )


def _next_position(db: Session, parent_id: int) -> int:
    """The position after the last of a parent's linked tasks."""
    last = db.query(func.max(TaskModel.position)).filter(TaskModel.parent_task_id == parent_id).scalar()
    return 0 if last is None else last + 1


def create_task(db: Session, task: TaskCreate, user_id: str):
    """Create and persist a new task, auto-creating any new tags."""
    now = datetime.now(UTC)
    priority = task.priority
    if priority is not None:
        _validate_priority_unique(db, user_id, priority)
    position = None
    if task.parent_task_id is not None:
        parent = _get_parent(db, task.parent_task_id, user_id)
        _validate_link(db, parent, None, user_id)
        position = _next_position(db, parent.id)

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
        position=position,
        reminder_minutes=task.reminder_minutes,
        user_id=user_id,
    )
    set_item_tags(db, db_task, task.tags, user_id)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


def delete_task(db: Session, task_id: int, user_id: str):
    """Move a task and its subtasks to the trash. Returns None if not found.

    The subtasks get the parent's deleted_at, which is how restore_task and
    purge_task tell them apart from subtasks deleted on their own.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = datetime.now(UTC)
    for t in [task, *_descendants(db, task, user_id)]:
        t.deleted_at = stamp
    purge_expired(db)
    db.commit()
    db.refresh(task)
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
    "due_time", "estimated_time", "parent_task_id", "reminder_minutes",
)


def update_task(db: Session, task_id: int, task: TaskCreate, user_id: str):
    """Replace all fields of an existing task, except its position. Returns None if not found."""
    db_task = get_owned(db, TaskModel, task_id, user_id)
    if not db_task:
        return None

    # Only a changed link is checked, so re-saving a task under its current
    # parent works even for links made before the one-level rule.
    if task.parent_task_id != db_task.parent_task_id:
        if task.parent_task_id is None:
            db_task.position = None
        else:
            parent = _get_parent(db, task.parent_task_id, user_id)
            _validate_link(db, parent, task_id, user_id)
            db_task.position = _next_position(db, parent.id)

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


def _descendants(db: Session, task: TaskModel, user_id: str, deleted: bool = False) -> list[TaskModel]:
    """Every live subtask below a task, at any depth, or every deleted one when deleted=True."""
    found: list[TaskModel] = []
    seen = {task.id}
    frontier = [task.id]
    while frontier:
        children = (
            db.query(TaskModel)
            .filter(
                TaskModel.parent_task_id.in_(frontier),
                TaskModel.user_id == user_id,
                deleted_filter(TaskModel, deleted),
            )
            .all()
        )
        children = [c for c in children if c.id not in seen]
        seen.update(c.id for c in children)
        found.extend(children)
        frontier = [c.id for c in children]
    return found


def _hidden_ancestors(db: Session, task: TaskModel, user_id: str, field: str) -> list[TaskModel]:
    """The unbroken chain of parents directly above a task with `field` (archived_at or deleted_at) set."""
    found: list[TaskModel] = []
    seen = {task.id}
    parent_id = task.parent_task_id
    while parent_id is not None and parent_id not in seen:
        parent = get_owned(db, TaskModel, parent_id, user_id, trashed=field == "deleted_at")
        if not parent or getattr(parent, field) is None:
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
    comes back under a hidden parent. A restored task whose priority was taken
    by another task while it was archived comes back without a priority rather
    than duplicating it.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    if task.archived_at is not None:
        stamp = task.archived_at
        restored = [
            *_hidden_ancestors(db, task, user_id, "archived_at"),
            task,
            *[t for t in _descendants(db, task, user_id) if t.archived_at == stamp],
        ]
        _bring_back(db, restored, "archived_at", user_id)
        db.commit()
        db.refresh(task)
    return task


def _bring_back(db: Session, tasks: list[TaskModel], field: str, user_id: str) -> None:
    """Clear `field` (archived_at or deleted_at) on tasks coming back into view.

    A task whose priority was taken by another task while it was hidden
    comes back without a priority rather than duplicating it.
    """
    for t in tasks:
        setattr(t, field, None)
        db.flush()
        if t.priority is not None and _priority_taken(db, user_id, t.priority, exclude_task_id=t.id):
            t.priority = None


def restore_task(db: Session, task_id: int, user_id: str):
    """Restore a task from the trash with the subtasks deleted along with it. Returns None if not in the trash.

    Mirrors unarchive_task: subtasks deleted separately, before the parent,
    stay in the trash; deleted ancestors are restored too (not their other
    subtasks), so the task never comes back under a hidden parent; and a
    priority taken in the meantime is dropped. A task deleted while archived
    is restored to the archive.
    """
    task = get_trashed(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = task.deleted_at
    restored = [
        *_hidden_ancestors(db, task, user_id, "deleted_at"),
        task,
        *[t for t in _descendants(db, task, user_id, deleted=True) if t.deleted_at == stamp],
    ]
    _bring_back(db, restored, "deleted_at", user_id)
    db.commit()
    db.refresh(task)
    return task


def purge_task(db: Session, task_id: int, user_id: str):
    """Permanently delete a task in the trash, with the subtasks deleted along with it.

    Returns None if not in the trash. Subtasks deleted on their own stay in
    the trash, detached from the purged parent.
    """
    task = get_trashed(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = task.deleted_at
    for t in [task, *[t for t in _descendants(db, task, user_id, deleted=True) if t.deleted_at == stamp]]:
        db.delete(t)
    db.commit()
    return task


def set_linked_tasks(db: Session, task_id: int, children: list[LinkedTaskRef], user_id: str):
    """Save a task's linked tasks in one transaction. Returns (parent, children), or None if not found.

    `children` lists every active linked task in order. Existing tasks are
    linked (moved from another parent if they have one) and new titles are
    created with the parent's tags and no due date or priority; each gets its
    index as its position. Active linked tasks left out are unlinked, not
    deleted. Archived ones stay linked, since the client never sees them.
    """
    parent = get_owned(db, TaskModel, task_id, user_id)
    if not parent:
        return None
    ids = [ref.id for ref in children if ref.id is not None]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="A task can only be listed once")

    now = datetime.now(UTC)
    linked: list[TaskModel] = []
    for position, ref in enumerate(children):
        if ref.id is None:
            _validate_link(db, parent, None, user_id)
            child = TaskModel(
                title=ref.title,
                created_date=now,
                user_id=user_id,
                tags=[t for t in parent.tags if t.deleted_at is None],
            )
            db.add(child)
        else:
            child = get_owned(db, TaskModel, ref.id, user_id)
            if not child:
                raise HTTPException(status_code=400, detail=f"Task with ID {ref.id} not found")
            if child.archived_at is not None:
                raise HTTPException(
                    status_code=400, detail=f"Task {ref.id} is archived; restore it before linking it"
                )
            _validate_link(db, parent, child.id, user_id)
        child.parent_task_id = parent.id
        child.position = position
        linked.append(child)
    db.flush()

    kept = {c.id for c in linked}
    dropped = (
        db.query(TaskModel)
        .filter(
            TaskModel.parent_task_id == parent.id,
            TaskModel.user_id == user_id,
            deleted_filter(TaskModel, False),
            archived_filter(TaskModel, False),
            TaskModel.id.notin_(kept),
        )
        .all()
    )
    for t in dropped:
        t.parent_task_id = None
        t.position = None

    db.commit()
    db.refresh(parent)
    for c in linked:
        db.refresh(c)
    return parent, linked
