"""
Tasks added from the Canvas panel ("Add to tasks"), which follow their Canvas
assignment.

Such a task has canvas_assignment_id and canvas_html_url set. sync() reads
each open linked task's assignment from Canvas and reports whether it has
been submitted, so the frontend can offer to complete the task; it never
completes a task itself. When the assignment's due date differs from the one
the last sync saw (canvas_due_at), the task's due_date and due_time are moved
to it, in the caller's time zone, to the minute (an assignment with no due
date clears them). A due date the user changed while Canvas's stayed the
same is left alone.

The course comes from canvas_html_url (/courses/{id}/...), which must be on
the connection's Canvas host. Each course's assignments are read through
canvas_data_crud.read(), so they share the panel's cache, its fetch limits
and its handling of a rejected token. A sync reads at most MAX_SYNC_COURSES
courses. A task whose course or assignment can't be read comes back with
synced=false and is left alone: no course in its link, a course or
assignment Canvas no longer shows, a course past the cap, or Canvas busy
or down partway through. If Canvas is busy or down before any course is
read, the error is raised instead.

With a calendar feed connection, sync() reads the feed once (sharing the
overview's cache) and matches each open task that follows an assignment by
its feed UID, event-assignment-<canvas_assignment_id>. Only due dates sync: a feed has no submissions, so
submitted is always false. A task whose item isn't in the feed comes back
with synced=false and is left alone.
"""

import re
from datetime import date, datetime, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.canvas_client import CanvasForbidden, CanvasNotFound, CanvasRateLimited, CanvasUnavailable
from app.crud import canvas_data_crud, canvas_sample
from app.crud.base import archived_filter, deleted_filter
from app.crud.canvas_crud import get_connection
from app.crud.canvas_data_crud import CanvasNotConnected, canvas_url
from app.models.task_model import Task as TaskModel
from app.schemas.canvas_schema import CanvasAssignment, CanvasItem, CanvasLinkedTaskSync

MAX_SYNC_COURSES = 20
_COURSE_PATH = re.compile(r"/courses/(\d+)(?:/|$)")


def _open_linked_tasks(db: Session, user_id: str, task_ids: list[int] | None = None) -> list[TaskModel]:
    """The user's open (not completed, archived or deleted) tasks that follow a Canvas assignment."""
    q = db.query(TaskModel).filter(
        TaskModel.user_id == user_id,
        TaskModel.canvas_assignment_id.is_not(None),
        TaskModel.completed.is_not(True),
        archived_filter(TaskModel, False),
        deleted_filter(TaskModel, False),
    )
    if task_ids is not None:
        q = q.filter(TaskModel.id.in_(task_ids))
    return q.order_by(TaskModel.id).all()


def course_id_of(base_url: str, html_url: str | None) -> int | None:
    """The course id in a link on the school's Canvas host, or None."""
    link = canvas_url(base_url, html_url)
    if link is None:
        return None
    match = _COURSE_PATH.match(urlsplit(link).path)
    return int(match.group(1)) if match else None


def _unsynced(task_id: int) -> CanvasLinkedTaskSync:
    return CanvasLinkedTaskSync(task_id=task_id, synced=False)


def _apply(task: TaskModel, assignment: CanvasItem, zone: ZoneInfo) -> CanvasLinkedTaskSync:
    """Move the task's due date to the assignment's if Canvas changed it since the last sync, and report its state."""
    changed = False
    if assignment.due_at != task.canvas_due_at:
        if assignment.due_at is None:
            due_date, due_time = None, None
        else:
            local = assignment.due_at.astimezone(zone)
            due_date, due_time = local.date(), local.time().replace(second=0, microsecond=0)
        current_date = task.due_date.date() if isinstance(task.due_date, datetime) else task.due_date
        current_time = task.due_time.replace(second=0, microsecond=0) if task.due_time else None
        changed = (due_date, due_time) != (current_date, current_time)
        if changed:
            task.due_date = datetime.combine(due_date, time()) if due_date else None
            task.due_time = due_time
        task.canvas_due_at = assignment.due_at
    return CanvasLinkedTaskSync(
        task_id=task.id,
        synced=True,
        submitted=assignment.submission is not None and assignment.submission.submitted,
        due_at=assignment.due_at,
        due_changed=changed,
    )


def sync(db: Session, user_id: str, zone: ZoneInfo, now: datetime, refresh: bool = False) -> dict:
    """Sync the user's open linked tasks with Canvas. See the module docstring.

    Returns the results with fetched_at (the oldest course read), busy and
    stale, like the panel's read routes.
    """
    connection = get_connection(db, user_id)
    if connection is not None and connection.connection_type == "feed":
        return _sync_feed(db, user_id, zone, now, refresh)
    links = [(t.id, t.canvas_assignment_id, t.canvas_html_url) for t in _open_linked_tasks(db, user_id)]
    if not links:
        return {"fetched_at": now, "results": []}
    row = get_connection(db, user_id)
    if row is None:
        raise CanvasNotConnected("Canvas isn't connected.")
    base_url = row.base_url
    course_of = {task_id: course_id_of(base_url, html_url) for task_id, _, html_url in links}
    courses = sorted({c for c in course_of.values() if c is not None})[:MAX_SYNC_COURSES]

    # Canvas is read before any task changes: read() commits the session.
    assignments: dict[tuple[int, int], CanvasAssignment] = {}
    reads: list[canvas_data_crud.Read] = []
    error: Exception | None = None
    for course_id in courses:
        try:
            result = canvas_data_crud.read(
                db, user_id, "assignments", (course_id,), canvas_data_crud.fetch_assignments(course_id), now,
                refresh=refresh,
            )
        except (CanvasNotFound, CanvasForbidden):
            continue
        except (CanvasUnavailable, HTTPException) as exc:
            # Canvas is down or busy, or kanso's own fetch limit is reached:
            # stop calling Canvas and report what was read so far.
            error = exc
            break
        reads.append(result)
        assignments.update({(course_id, a.id): a for a in result.data})
    if error is not None and not reads:
        raise error

    results = []
    tasks = {t.id: t for t in _open_linked_tasks(db, user_id, [task_id for task_id, _, _ in links])}
    for task_id, assignment_id, _ in links:
        task = tasks.get(task_id)
        assignment = assignments.get((course_of[task_id], assignment_id))
        if task is None or assignment is None or task.canvas_assignment_id != assignment_id:
            results.append(_unsynced(task_id))
        else:
            results.append(_apply(task, assignment, zone))
    db.commit()
    return {
        "fetched_at": min((r.fetched_at for r in reads), default=now),
        "busy": any(r.busy for r in reads) or isinstance(error, CanvasRateLimited),
        "stale": any(r.stale for r in reads) or error is not None,
        "results": results,
    }


def _feed_uid(task: TaskModel) -> str:
    """The feed UID of the assignment a task follows."""
    return f"event-assignment-{task.canvas_assignment_id}"


def _sync_feed(db: Session, user_id: str, zone: ZoneInfo, now: datetime, refresh: bool) -> dict:
    """sync() against the calendar feed. See the module docstring."""
    links = [(t.id, _feed_uid(t)) for t in _open_linked_tasks(db, user_id)]
    if not links:
        return {"fetched_at": now, "results": []}
    # The feed is read before any task changes: read() commits the session.
    result = canvas_data_crud.read(db, user_id, "feed", (), canvas_data_crud.fetch_feed, now, refresh=refresh)
    items = {item.uid: item for item in canvas_data_crud.feed_items(result.data, zone)}

    results = []
    tasks = {t.id: t for t in _open_linked_tasks(db, user_id, [task_id for task_id, _ in links])}
    for task_id, uid in links:
        task = tasks.get(task_id)
        item = items.get(uid)
        if task is None or item is None or _feed_uid(task) != uid:
            results.append(_unsynced(task_id))
        else:
            results.append(_apply(task, item, zone))
    db.commit()
    return {"fetched_at": result.fetched_at, "busy": result.busy, "stale": result.stale, "results": results}


def sync_sample(db: Session, user_id: str, zone: ZoneInfo, today: date) -> list[CanvasLinkedTaskSync]:
    """sync() for a demo sandbox, against the sample assignments by id, without calling Canvas."""
    sample = canvas_sample.assignments_by_id(today, zone)
    results = []
    for task in _open_linked_tasks(db, user_id):
        assignment = sample.get(task.canvas_assignment_id)
        results.append(_unsynced(task.id) if assignment is None else _apply(task, assignment, zone))
    db.commit()
    return results
