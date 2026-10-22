"""
Tasks: listing, creating, updating, completing and setting linked tasks.

Archiving and the trash are in task_lifecycle_crud, walking the
parent/subtask tree in task_tree, and the priority and Canvas-link rules
in task_rules.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.crud.base import archived_filter, deleted_filter, get_owned
from app.crud.canvas_crud import get_connection
from app.crud.tag_crud import set_item_tags
from app.crud.task_rules import drop_taken_canvas_link, validate_canvas_unique, validate_priority_unique
from app.crud.task_tree import descendants, get_parent, next_position, validate_link
from app.models.task_model import Task as TaskModel
from app.schemas.task_schema import LinkedTaskRef, TaskCreate


def get_tasks(db: Session, user_id: str, archived: bool = False) -> list[TaskModel]:
    """Return the user's active tasks, or only their archived ones when archived=True. Deleted tasks are left out.

    Each task's parent is loaded alongside for its parent_task summary, since
    an archived subtask's parent is usually not in the same list.
    """
    return (
        db.query(TaskModel)
        .options(selectinload(TaskModel.parent_task))
        .filter(TaskModel.user_id == user_id, deleted_filter(TaskModel, False), archived_filter(TaskModel, archived))
        .all()
    )


def _canvas_source(db: Session, user_id: str) -> str:
    """The connection a task linked now takes its Canvas due values from: "feed" or "token"."""
    connection = get_connection(db, user_id)
    return "feed" if connection is not None and connection.connection_type == "feed" else "token"


def create_task(db: Session, task: TaskCreate, user_id: str) -> TaskModel:
    """Create and persist a new task, auto-creating any new tags."""
    now = datetime.now(UTC)
    priority = task.priority
    if priority is not None:
        validate_priority_unique(db, user_id, priority)
    if task.canvas_assignment_id is not None and not task.completed:
        validate_canvas_unique(db, user_id, task.canvas_assignment_id)
    position = None
    if task.parent_task_id is not None:
        parent = get_parent(db, task.parent_task_id, user_id)
        validate_link(db, parent, None, user_id)
        position = next_position(db, parent.id)

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
        canvas_assignment_id=task.canvas_assignment_id,
        canvas_html_url=task.canvas_html_url,
        canvas_due_at=task.canvas_due_at if task.canvas_assignment_id is not None else None,
        canvas_due_date=task.canvas_due_date if task.canvas_assignment_id is not None else None,
        canvas_due_source=_canvas_source(db, user_id) if task.canvas_assignment_id is not None else None,
        user_id=user_id,
    )
    set_item_tags(db, db_task, task.tags, user_id)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


def update_task_status(db: Session, task_id: int, user_id: str, with_linked: bool = False) -> TaskModel | None:
    """Toggle a task's completed status and update completed_date accordingly.

    With with_linked=True, completing a task also completes its open, active
    linked tasks at every depth in the same transaction. Reopening a task
    leaves them as they are.
    """
    task = get_owned(db, TaskModel, task_id, user_id)
    if not task:
        return None
    now = datetime.now(UTC)
    task.completed = not task.completed
    task.completed_date = now if task.completed else None
    drop_taken_canvas_link(db, user_id, task)
    if with_linked and task.completed:
        for child in descendants(db, task, user_id):
            if not child.completed and child.archived_at is None:
                child.completed = True
                child.completed_date = now
    db.commit()
    db.refresh(task)
    return task


_TASK_UPDATE_FIELDS = (
    "title", "description", "category", "completed", "due_date",
    "due_time", "estimated_time", "parent_task_id", "reminder_minutes",
)
# Replaced only when the payload includes them, so a client that predates the
# Canvas link doesn't unlink a task by saving it.
_CANVAS_FIELDS = ("canvas_assignment_id", "canvas_html_url")


def update_task(db: Session, task_id: int, task: TaskCreate, user_id: str) -> TaskModel | None:
    """Replace all fields of an existing task, except its position. Returns None if not found.

    The Canvas link fields are replaced only when the payload includes them.
    """
    db_task = get_owned(db, TaskModel, task_id, user_id)
    if not db_task:
        return None

    canvas_fields = [field for field in _CANVAS_FIELDS if field in task.model_fields_set]
    assignment_id = (
        task.canvas_assignment_id if "canvas_assignment_id" in canvas_fields else db_task.canvas_assignment_id
    )
    relinked = assignment_id != db_task.canvas_assignment_id
    # Only a newly set or reopened link is checked, so a task already
    # following an assignment re-saves as it is.
    if assignment_id is not None and not task.completed and (relinked or db_task.completed):
        validate_canvas_unique(db, user_id, assignment_id, exclude_task_id=task_id)
    if relinked:
        db_task.canvas_due_at = task.canvas_due_at if assignment_id is not None else None
        db_task.canvas_due_date = task.canvas_due_date if assignment_id is not None else None
        db_task.canvas_due_source = _canvas_source(db, user_id) if assignment_id is not None else None

    # Only a changed link is checked, so re-saving a task under its current
    # parent never trips the loop or archive checks.
    if task.parent_task_id != db_task.parent_task_id:
        if task.parent_task_id is None:
            db_task.position = None
        else:
            parent = get_parent(db, task.parent_task_id, user_id)
            validate_link(db, parent, task_id, user_id)
            db_task.position = next_position(db, parent.id)

    if task.priority is not None:
        validate_priority_unique(db, user_id, task.priority, exclude_task_id=task_id)
    db_task.priority = task.priority

    was_completed = db_task.completed

    for field in (*_TASK_UPDATE_FIELDS, *canvas_fields):
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


def set_linked_tasks(
    db: Session, task_id: int, children: list[LinkedTaskRef], user_id: str
) -> tuple[TaskModel, list[TaskModel]] | None:
    """Save a task's linked tasks in one transaction. Returns (parent, children), or None if not found.

    `children` lists every active linked task in order. Existing tasks are
    linked (moved from another parent if they have one) and new titles are
    created with the parent's tags and no due date or priority; each gets its
    index as its position. Active linked tasks left out are unlinked, not
    deleted. Archived ones stay linked, since the client never sees them.
    Only new links are checked against the rules, so links already under this
    task always re-save.
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
            validate_link(db, parent, None, user_id)
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
            if child.parent_task_id != parent.id:
                validate_link(db, parent, child.id, user_id)
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
