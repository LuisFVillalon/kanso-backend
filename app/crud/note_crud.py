from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.crud.base import archived_filter, get_owned, set_archived
from app.crud.tag_crud import set_item_tags
from app.models.note_model import Note as NoteModel
from app.schemas.note_schema import NoteCreate, NoteUpdate


def get_notes(db: Session, user_id: str, archived: bool = False):
    """Return the user's active notes, or only their archived ones when archived=True."""
    return db.query(NoteModel).filter(NoteModel.user_id == user_id, archived_filter(NoteModel, archived)).all()


def create_note(db: Session, note: NoteCreate, user_id: str):
    """Create and persist a new note, auto-creating any new tags."""
    now = datetime.now(UTC)
    db_note = NoteModel(
        title=note.title,
        content=note.content,
        user_id=user_id,
        created_date=now,
        updated_date=now,
    )
    set_item_tags(db, db_note, note.tags, user_id)
    db.add(db_note)
    db.commit()
    db.refresh(db_note)
    return db_note


def update_note(db: Session, note_id: int, note: NoteUpdate, user_id: str):
    """Partially update a note's title, content, and tags. Returns None if not found."""
    db_note = get_owned(db, NoteModel, note_id, user_id)
    if not db_note:
        return None

    if note.title is not None:
        db_note.title = note.title
    if note.content is not None:
        db_note.content = note.content
    db_note.updated_date = datetime.now(UTC)

    if note.tags is not None:
        set_item_tags(db, db_note, note.tags, user_id)
        db.flush()

    db.commit()
    db.refresh(db_note)
    return db_note


def delete_note(db: Session, note_id: int, user_id: str):
    """Delete a note. Returns the deleted record, or None if not found."""
    db_note = get_owned(db, NoteModel, note_id, user_id)
    if not db_note:
        return None
    db.delete(db_note)
    db.commit()
    return db_note


def archive_note(db: Session, note_id: int, user_id: str):
    """Archive a note. Returns None if not found."""
    return set_archived(db, NoteModel, note_id, user_id, archived=True)


def unarchive_note(db: Session, note_id: int, user_id: str):
    """Restore an archived note. Returns None if not found."""
    return set_archived(db, NoteModel, note_id, user_id, archived=False)
