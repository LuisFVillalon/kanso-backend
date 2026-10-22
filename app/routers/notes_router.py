from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.note_crud import (
    archive_note,
    create_note,
    delete_note,
    get_notes,
    purge_note,
    restore_note,
    unarchive_note,
    update_note,
)
from app.database.database import get_db
from app.schemas.note_schema import Note, NoteCreate, NoteUpdate

router = APIRouter()


@router.get("/get-notes", response_model=list[Note])
def read_notes(
    archived: bool = Query(False, description="Return only archived notes instead of active ones."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's active notes, or their archived ones with ?archived=true."""
    return get_notes(db, current_user.id, archived=archived)


@router.post("/create-note", response_model=Note, status_code=status.HTTP_201_CREATED)
def create_new_note(
    note: NoteCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new note for the authenticated user."""
    return create_note(db, note, current_user.id)


@router.put("/update-note/{note_id}", response_model=Note)
def update_note_by_id(
    note_id: int,
    payload: NoteUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partially update a note's title, content, and tags."""
    return require_found(update_note(db, note_id, payload, current_user.id), "Note not found")


@router.delete("/del-note/{note_id}", response_model=Note)
def delete_note_by_id(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move a note to the trash, where it can be restored for 30 days."""
    return require_found(delete_note(db, note_id, current_user.id), "Note not found")


@router.patch("/archive-note/{note_id}", response_model=Note)
def archive_note_by_id(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive a note."""
    return require_found(archive_note(db, note_id, current_user.id), "Note not found")


@router.patch("/unarchive-note/{note_id}", response_model=Note)
def unarchive_note_by_id(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore an archived note."""
    return require_found(unarchive_note(db, note_id, current_user.id), "Note not found")


@router.patch("/restore-note/{note_id}", response_model=Note)
def restore_note_by_id(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore a note from the trash, with its editing time."""
    return require_found(restore_note(db, note_id, current_user.id), "Note not found in the trash")


@router.delete("/purge-note/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def purge_note_by_id(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a note that is in the trash, without waiting out the 30 days."""
    require_found(purge_note(db, note_id, current_user.id), "Note not found in the trash")
