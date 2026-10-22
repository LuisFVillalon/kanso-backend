from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.crud.base import get_owned, list_owned, set_archived
from app.crud.tag_crud import set_item_tags
from app.crud.trash_crud import move_to_trash, purge_from_trash, restore_from_trash
from app.models.note_model import Note as NoteModel
from app.schemas.note_schema import NoteCreate, NoteUpdate


def get_notes(db: Session, user_id: str, archived: bool = False) -> list[NoteModel]:
    """Return the user's active notes, or only their archived ones when archived=True. Deleted notes are left out."""
    return list_owned(db, NoteModel, user_id, archived)


def create_note(db: Session, note: NoteCreate, user_id: str) -> NoteModel:
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


def update_note(db: Session, note_id: int, note: NoteUpdate, user_id: str) -> NoteModel | None:
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


def delete_note(db: Session, note_id: int, user_id: str) -> NoteModel | None:
    """Move a note to the trash, keeping its editing sessions. Returns None if not found."""
    return move_to_trash(db, NoteModel, note_id, user_id)


def archive_note(db: Session, note_id: int, user_id: str) -> NoteModel | None:
    """Archive a note. Returns None if not found."""
    return set_archived(db, NoteModel, note_id, user_id, archived=True)


def unarchive_note(db: Session, note_id: int, user_id: str) -> NoteModel | None:
    """Restore an archived note. Returns None if not found."""
    return set_archived(db, NoteModel, note_id, user_id, archived=False)


def restore_note(db: Session, note_id: int, user_id: str) -> NoteModel | None:
    """Restore a note from the trash. Returns None if not in the trash."""
    return restore_from_trash(db, NoteModel, note_id, user_id)


def purge_note(db: Session, note_id: int, user_id: str) -> NoteModel | None:
    """Permanently delete a note in the trash, with its editing sessions. Returns None if not in the trash."""
    return purge_from_trash(db, NoteModel, note_id, user_id)
