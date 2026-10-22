"""
Rules a user's open tasks keep between them: no two active tasks share a
priority, and no two open tasks follow the same Canvas assignment.

Creating or updating a task that would break one is refused. A task coming
back into view (reopened, unarchived or restored) instead gives up the
priority or Canvas link another task took over meanwhile.
"""

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_
from sqlalchemy.orm import Session

from app.crud.base import active_filter
from app.models.task_model import Task as TaskModel


def open_task_filter() -> ColumnElement[bool]:
    """Filter clause selecting open tasks: not completed, archived or deleted."""
    return and_(TaskModel.completed.is_not(True), active_filter(TaskModel))


def priority_taken(db: Session, user_id: str, priority: int, exclude_task_id: int | None = None) -> bool:
    """True if another active task for this user already has the same priority.

    Archived and deleted tasks keep their priority but don't hold it: they're
    hidden from the user, who would otherwise be blocked by a task they can't see.
    """
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.priority == priority,
        active_filter(TaskModel),
    )
    if exclude_task_id is not None:
        q = q.filter(TaskModel.id != exclude_task_id)
    return q.first() is not None


def validate_priority_unique(db: Session, user_id: str, priority: int, exclude_task_id: int | None = None) -> None:
    """Raise 400 if another active task for this user already has the same priority."""
    if priority_taken(db, user_id, priority, exclude_task_id):
        raise HTTPException(
            status_code=400,
            detail=f"Priority {priority} is already assigned to another task.",
        )


def open_canvas_task(
    db: Session, user_id: str, canvas_assignment_id: int, exclude_task_id: int | None = None
) -> TaskModel | None:
    """The user's open (not completed, archived or deleted) task following this Canvas assignment, if any."""
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.canvas_assignment_id == canvas_assignment_id,
        open_task_filter(),
    )
    if exclude_task_id is not None:
        q = q.filter(TaskModel.id != exclude_task_id)
    return q.order_by(TaskModel.id).first()


def validate_canvas_unique(
    db: Session, user_id: str, canvas_assignment_id: int, exclude_task_id: int | None = None
) -> None:
    """Raise 409, with the existing task's id, if an open task already follows this Canvas assignment."""
    existing = open_canvas_task(db, user_id, canvas_assignment_id, exclude_task_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "canvas_task_exists",
                "message": "An open task already follows this Canvas assignment.",
                "task_id": existing.id,
            },
        )


def drop_taken_canvas_link(db: Session, user_id: str, task: TaskModel) -> None:
    """Unlink an open task from its Canvas assignment if another open task already follows it.

    Like a taken priority, for tasks reopened, unarchived or restored while
    another task took over the assignment. The page link is kept.
    """
    if (
        task.canvas_assignment_id is not None
        and not task.completed
        and task.archived_at is None
        and task.deleted_at is None
        and open_canvas_task(db, user_id, task.canvas_assignment_id, exclude_task_id=task.id)
    ):
        task.canvas_assignment_id = None
        task.canvas_due_at = None
        task.canvas_due_date = None
        task.canvas_due_source = None


def drop_taken_priority(db: Session, user_id: str, task: TaskModel) -> None:
    """Clear a task's priority if another active task took it while this one was hidden."""
    if task.priority is not None and priority_taken(db, user_id, task.priority, exclude_task_id=task.id):
        task.priority = None
