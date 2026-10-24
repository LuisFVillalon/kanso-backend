"""
The trash: deleted tasks, notes, tags, habits and events, kept for TRASH_RETENTION.

Deleting one of these sets its deleted_at instead of removing the row, so
its tags, habit logs and note sessions survive for a restore. One deleted
occurrence of a repeating event is its exception row's deleted_at (see
event_crud). Every normal
list, lookup and stat leaves deleted rows out (see base.get_owned).

Expired rows are purged for every user at once, with no scheduler:
purge_expired runs at startup and whenever anyone opens the trash, restores
or purges from it, or deletes something, so an item is removed for good
even if its owner never comes back. A row past retention is already
invisible everywhere (even to the trash and restore), so when its physical
removal happens isn't observable; deleting the account removes it at once
regardless.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.crud.base import TRASH_RETENTION, get_owned, trash_cutoff
from app.models.event_model import Event, EventException
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.tag_model import Tag
from app.models.task_model import Task
from app.schemas.task_schema import TaskParent
from app.schemas.trash_schema import TrashItem

TRASHABLE_MODELS = {"task": Task, "note": Note, "tag": Tag, "habit": Habit, "event": Event}


def purge_expired(db: Session) -> None:
    """Permanently delete every user's rows deleted more than TRASH_RETENTION ago. Does not commit.

    One delete per table, served by its deleted_at index. Tag links, habit
    logs, note sessions and event exceptions go with their rows (ON DELETE
    CASCADE); subtasks of a purged task are detached first, found by the
    parent_task_id index. An expired event occurrence is cancelled instead,
    so its series doesn't bring it back.
    Concurrent runs are safe: a row another run already deleted is just
    skipped.
    """
    cutoff = trash_cutoff()
    expired_task_ids = db.scalars(select(Task.id).where(Task.deleted_at <= cutoff)).all()
    if expired_task_ids:
        db.execute(
            update(Task)
            .where(Task.parent_task_id.in_(expired_task_ids))
            .values(parent_task_id=None)
            .execution_options(synchronize_session="fetch")
        )
    for model in TRASHABLE_MODELS.values():
        db.execute(delete(model).where(model.deleted_at <= cutoff).execution_options(synchronize_session="fetch"))
    db.execute(
        update(EventException)
        .where(EventException.deleted_at <= cutoff)
        .values(deleted_at=None, cancelled=True)
        .execution_options(synchronize_session="fetch")
    )


def get_trash(db: Session, user_id: str) -> list[TrashItem]:
    """List the user's deleted items, newest first."""
    purge_expired(db)
    db.commit()
    items = []
    for k, model in TRASHABLE_MODELS.items():
        query = db.query(model).filter(model.user_id == user_id, model.deleted_at.isnot(None))
        if k == "task":
            query = query.options(selectinload(Task.parent_task))
        rows = query.all()
        items.extend(
            TrashItem(
                kind=k,
                id=row.id,
                title=row.name if k == "tag" else row.title,
                deleted_at=row.deleted_at,
                purge_at=row.deleted_at + TRASH_RETENTION,
                parent_task_id=row.parent_task_id if k == "task" else None,
                parent_task=TaskParent.model_validate(row.parent_task) if k == "task" and row.parent_task else None,
            )
            for row in rows
        )
    # Occurrences deleted on their own, while their series is live (a
    # deleted series stands for all of its occurrences).
    occurrences = (
        db.query(EventException, Event.title)
        .join(Event, Event.id == EventException.event_id)
        .filter(Event.user_id == user_id, Event.deleted_at.is_(None), EventException.deleted_at.isnot(None))
        .all()
    )
    items.extend(
        TrashItem(
            kind="event",
            id=exc.event_id,
            title=exc.title or series_title,
            deleted_at=exc.deleted_at,
            purge_at=exc.deleted_at + TRASH_RETENTION,
            occurrence_date=exc.occurrence_date,
        )
        for exc, series_title in occurrences
    )
    return sorted(items, key=lambda item: item.deleted_at, reverse=True)


def get_trashed(db: Session, model, record_id: int, user_id: str):
    """Purge expired rows, then fetch one row still in the trash. Returns None if not there."""
    purge_expired(db)
    db.commit()
    return get_owned(db, model, record_id, user_id, trashed=True)


def move_to_trash(db: Session, model, record_id: int, user_id: str):
    """Move a note, tag or habit to the trash, its links kept for a restore. Returns None if not found."""
    row = get_owned(db, model, record_id, user_id)
    if not row:
        return None
    row.deleted_at = datetime.now(UTC)
    purge_expired(db)
    db.commit()
    db.refresh(row)
    return row


def restore_from_trash(db: Session, model, record_id: int, user_id: str):
    """Bring a note, tag or habit back from the trash, links intact. Returns None if not in the trash.

    It comes back as it was, so an item deleted while archived is restored
    to the archive.
    """
    row = get_trashed(db, model, record_id, user_id)
    if not row:
        return None
    row.deleted_at = None
    db.commit()
    db.refresh(row)
    return row


def purge_from_trash(db: Session, model, record_id: int, user_id: str):
    """Permanently delete a note, tag or habit that is in the trash. Returns None if not in the trash."""
    row = get_trashed(db, model, record_id, user_id)
    if not row:
        return None
    db.delete(row)
    db.commit()
    return row
