"""
Linked tasks: walking and checking the parent/subtask tree.

A task's parent is parent_task_id, and links nest to any depth. Every walk
here is scoped to the owning user and stops on a repeated id, so a loop
already in the data can't hang it.
"""

from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import archived_filter, deleted_filter, get_owned
from app.models.task_model import Task as TaskModel


def get_parent(db: Session, parent_task_id: int, user_id: str) -> TaskModel:
    """The user's live task to link under, or 400 if it doesn't exist or belongs to a different user."""
    parent = get_owned(db, TaskModel, parent_task_id, user_id)
    if not parent:
        raise HTTPException(
            status_code=400,
            detail=f"Parent task with ID {parent_task_id} not found",
        )
    return parent


def ancestor_ids(db: Session, task: TaskModel, user_id: str) -> set[int]:
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


def validate_link(db: Session, parent: TaskModel, child_id: int | None, user_id: str) -> None:
    """Raise 400 unless a task (child_id, or None for a new one) can be linked under parent.

    Linked tasks nest to any depth, so a task can't be linked under itself
    or under any task below it, which would make a loop. Archived tasks
    can't take new linked tasks, since they're hidden from the lists the
    links would show in.
    """
    if child_id is not None:
        if child_id == parent.id:
            raise HTTPException(status_code=400, detail="A task cannot be its own parent")
        if child_id in ancestor_ids(db, parent, user_id):
            raise HTTPException(
                status_code=400, detail=f"Linking task {child_id} under task {parent.id} would create a loop"
            )
    if parent.archived_at is not None:
        raise HTTPException(
            status_code=400, detail=f"Task {parent.id} is archived; restore it before linking tasks to it"
        )


def next_position(db: Session, parent_id: int) -> int:
    """The position after the last of a parent's linked tasks."""
    last = db.query(func.max(TaskModel.position)).filter(TaskModel.parent_task_id == parent_id).scalar()
    return 0 if last is None else last + 1


def unlink_children(db: Session, task: TaskModel, user_id: str, archived: bool | None = None) -> None:
    """Make the task's live linked tasks standalone, or only the archived/active ones when archived is set.

    Only direct children are unlinked; anything below them stays with them.
    """
    q = db.query(TaskModel).filter(
        TaskModel.parent_task_id == task.id,
        TaskModel.user_id == user_id,
        deleted_filter(TaskModel, False),
    )
    if archived is not None:
        q = q.filter(archived_filter(TaskModel, archived))
    for child in q.all():
        child.parent_task_id = None
        child.position = None
    db.flush()


def descendants(db: Session, task: TaskModel, user_id: str, deleted: bool = False) -> list[TaskModel]:
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


HiddenField = Literal["archived_at", "deleted_at"]


def hidden_ancestors(db: Session, task: TaskModel, user_id: str, field: HiddenField) -> list[TaskModel]:
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
