"""
Archiving and trashing tasks, which carry their linked tasks along.

Archiving or deleting a task stamps the same archived_at or deleted_at on
its subtasks at every depth, unless linked="keep" unlinks them first. That
shared stamp is how unarchiving, restoring and purging tell the subtasks
hidden with the task apart from ones hidden on their own, which stay as
they are. Ancestors hidden above a task come back with it, so it never
returns under a hidden parent.
"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.crud.task_rules import drop_taken_canvas_link, drop_taken_priority
from app.crud.task_tree import HiddenField, descendants, hidden_ancestors, unlink_children
from app.crud.trash_crud import get_trashed, purge_expired
from app.models.task_model import Task as TaskModel

LinkedMode = Literal["keep", "cascade"]


def delete_task(db: Session, task_id: int, user_id: str, linked: LinkedMode = "cascade") -> TaskModel | None:
    """Move a task to the trash, with its subtasks unless linked="keep". Returns None if not found.

    The subtasks get the parent's deleted_at, which is how restore_task and
    purge_task tell them apart from subtasks deleted on their own. With
    linked="keep" its linked tasks, archived ones included, are unlinked
    first and stay as standalone tasks.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    if linked == "keep":
        unlink_children(db, task, user_id)
    stamp = datetime.now(UTC)
    for t in [task, *descendants(db, task, user_id)]:
        t.deleted_at = stamp
    purge_expired(db)
    db.commit()
    db.refresh(task)
    return task


def archive_task(db: Session, task_id: int, user_id: str, linked: LinkedMode = "cascade") -> TaskModel | None:
    """Archive a task, with its active subtasks unless linked="keep". Returns None if not found.

    The subtasks get the parent's archived_at, which is how unarchive_task
    tells them apart from subtasks archived on their own. With linked="keep"
    its active linked tasks are unlinked first and stay as standalone tasks;
    ones archived on their own stay linked. Idempotent: an already-archived
    task keeps its original archived_at.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    if linked == "keep":
        unlink_children(db, task, user_id, archived=False)
    stamp = task.archived_at or datetime.now(UTC)
    for t in [task, *descendants(db, task, user_id)]:
        if t.archived_at is None:
            t.archived_at = stamp
    db.commit()
    db.refresh(task)
    return task


def unarchive_task(db: Session, task_id: int, user_id: str) -> TaskModel | None:
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
            *hidden_ancestors(db, task, user_id, "archived_at"),
            task,
            *[t for t in descendants(db, task, user_id) if t.archived_at == stamp],
        ]
        _bring_back(db, restored, "archived_at", user_id)
        db.commit()
        db.refresh(task)
    return task


def _bring_back(db: Session, tasks: list[TaskModel], field: HiddenField, user_id: str) -> None:
    """Clear `field` (archived_at or deleted_at) on tasks coming back into view.

    A task whose priority was taken by another task while it was hidden
    comes back without a priority rather than duplicating it, and one whose
    Canvas assignment another open task took over comes back unlinked from it.
    """
    for t in tasks:
        setattr(t, field, None)
        db.flush()
        drop_taken_priority(db, user_id, t)
        drop_taken_canvas_link(db, user_id, t)


def restore_task(db: Session, task_id: int, user_id: str) -> TaskModel | None:
    """Restore a task from the trash with the subtasks deleted along with it. Returns None if not in the trash.

    Mirrors unarchive_task: subtasks deleted separately, before the parent,
    stay in the trash; deleted ancestors are restored too (not their other
    subtasks), so the task never comes back under a hidden parent; and a
    priority taken in the meantime is dropped. A task deleted while archived
    is restored to the archive. A task comes back under its old parent
    wherever that parent sits now; one whose parent was purged comes back on
    its own, since the purge detached it.
    """
    task = get_trashed(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = task.deleted_at
    restored = [
        *hidden_ancestors(db, task, user_id, "deleted_at"),
        task,
        *[t for t in descendants(db, task, user_id, deleted=True) if t.deleted_at == stamp],
    ]
    _bring_back(db, restored, "deleted_at", user_id)
    db.commit()
    db.refresh(task)
    return task


def purge_task(db: Session, task_id: int, user_id: str) -> TaskModel | None:
    """Permanently delete a task in the trash, with the subtasks deleted along with it.

    Returns None if not in the trash. Subtasks deleted on their own stay in
    the trash, detached from the purged parent.
    """
    task = get_trashed(db, TaskModel, task_id, user_id)
    if not task:
        return None
    stamp = task.deleted_at
    for t in [task, *[t for t in descendants(db, task, user_id, deleted=True) if t.deleted_at == stamp]]:
        db.delete(t)
    db.commit()
    return task
