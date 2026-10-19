from datetime import UTC, datetime

from sqlalchemy.orm import Session


def get_owned[ModelT](db: Session, model: type[ModelT], record_id: int, user_id: str) -> ModelT | None:
    """Fetch a row by primary key, scoped to its owning user.

    Every user-owned table (tasks, notes, tags, habits, ...) is looked up the
    same way: match the id, then confirm user_id ownership. Centralizing it
    here means that check can't drift or get skipped in a new crud module.
    """
    return db.query(model).filter(model.id == record_id, model.user_id == user_id).first()


def archived_filter(model, archived: bool):
    """Filter clause selecting a model's archived rows, or its active ones (archived_at NULL)."""
    return model.archived_at.isnot(None) if archived else model.archived_at.is_(None)


def set_archived[ModelT](
    db: Session, model: type[ModelT], record_id: int, user_id: str, archived: bool
) -> ModelT | None:
    """Archive or restore an owned row. Returns None if not found.

    Idempotent: archiving an already-archived row keeps its original
    archived_at, and restoring an active row changes nothing.
    """
    row = get_owned(db, model, record_id, user_id)
    if not row:
        return None
    if archived and row.archived_at is None:
        row.archived_at = datetime.now(UTC)
    elif not archived:
        row.archived_at = None
    db.commit()
    db.refresh(row)
    return row
