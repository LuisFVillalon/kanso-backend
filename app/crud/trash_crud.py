"""
The trash: deleted tasks, notes, tags and habits, kept for TRASH_RETENTION.

Deleting one of these sets its deleted_at instead of removing the row, so
its tags, habit logs and note sessions survive for a restore. Every normal
list, lookup and stat leaves deleted rows out (see base.get_owned).

Expired rows are purged per user, with no scheduler: purge_expired runs
whenever the user opens the trash, restores or purges from it, or deletes
something. A row past retention is already invisible everywhere (even to
the trash and restore), so when its physical removal happens isn't
observable; deleting the account removes it at once regardless.
"""

from sqlalchemy.orm import Session

from app.crud.base import TRASH_RETENTION, get_owned, trash_cutoff
from app.models.habit_model import Habit
from app.models.note_model import Note
from app.models.tag_model import Tag
from app.models.task_model import Task
from app.schemas.trash_schema import TrashItem

TRASHABLE_MODELS = {"task": Task, "note": Note, "tag": Tag, "habit": Habit}


def purge_expired(db: Session, user_id: str) -> None:
    """Permanently delete the user's rows deleted more than TRASH_RETENTION ago. Flushes but does not commit.

    Rows go through the ORM rather than a bulk delete so a purged parent
    task's remaining subtasks get their parent_task_id cleared.
    """
    cutoff = trash_cutoff()
    for model in TRASHABLE_MODELS.values():
        for row in db.query(model).filter(model.user_id == user_id, model.deleted_at <= cutoff).all():
            db.delete(row)
        db.flush()


def get_trash(db: Session, user_id: str, kind: str | None = None) -> list[TrashItem]:
    """List the user's deleted items, newest first, optionally of one kind only."""
    purge_expired(db, user_id)
    db.commit()
    kinds = [kind] if kind else list(TRASHABLE_MODELS)
    items = []
    for k in kinds:
        model = TRASHABLE_MODELS[k]
        rows = db.query(model).filter(model.user_id == user_id, model.deleted_at.isnot(None)).all()
        items.extend(
            TrashItem(
                kind=k,
                id=row.id,
                title=row.name if k == "tag" else row.title,
                deleted_at=row.deleted_at,
                purge_at=row.deleted_at + TRASH_RETENTION,
                parent_task_id=row.parent_task_id if k == "task" else None,
            )
            for row in rows
        )
    return sorted(items, key=lambda item: item.deleted_at, reverse=True)


def get_trashed(db: Session, model, record_id: int, user_id: str):
    """Purge the user's expired rows, then fetch one row still in the trash. Returns None if not there."""
    purge_expired(db, user_id)
    db.commit()
    return get_owned(db, model, record_id, user_id, trashed=True)


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
