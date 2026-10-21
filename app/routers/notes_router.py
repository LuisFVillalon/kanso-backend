from datetime import datetime, timedelta
from datetime import time as time_type

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import parse_iso_date, require_date_range, require_found
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
from app.crud.note_session_crud import (
    end_session,
    get_time_by_note_for_datetime_range,
    get_time_by_note_for_range,
    get_total_time_by_note,
    get_total_time_seconds,
    reap_stale_sessions,
    start_session,
)
from app.database.database import get_db
from app.schemas.note_schema import Note, NoteCreate, NoteUpdate
from app.schemas.note_session_schema import NoteSession, NoteSessionReapResult, NoteSessionStart, NoteTimeSpent

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


@router.post("/note-session/start", response_model=NoteSession, status_code=status.HTTP_201_CREATED)
def start_note_session(
    payload: NoteSessionStart,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start a new editing session for a note. Close it via /note-session/end/{id}."""
    return require_found(start_session(db, payload.note_id, current_user.id), "Note not found")


@router.put("/note-session/end/{session_id}", response_model=NoteSession)
def end_note_session(
    session_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close an open editing session, recording its end time."""
    return require_found(end_session(db, session_id, current_user.id), "Session not found or already ended")


@router.get("/note-session/total/{note_id}", response_model=NoteTimeSpent)
def read_note_time_spent(
    note_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return total time (in seconds) spent editing a note, summed across all closed sessions."""
    return {"note_id": note_id, "total_seconds": get_total_time_seconds(db, note_id, current_user.id)}


@router.get("/note-session/totals", response_model=list[NoteTimeSpent])
def read_all_note_time_spent(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return total time (in seconds) spent per note, for every note with at least one closed session."""
    return [
        {"note_id": note_id, "total_seconds": total_seconds}
        for note_id, total_seconds in get_total_time_by_note(db, current_user.id)
    ]


@router.get("/note-session/totals-range", response_model=list[NoteTimeSpent])
def read_note_time_spent_for_range(
    start_date: str = Query(...),
    end_date: str = Query(...),
    utc_offset_minutes: int | None = Query(
        None,
        description=(
            "The caller's UTC offset as returned by JS Date.getTimezoneOffset() "
            "(minutes to add to local time to reach UTC; e.g. 420 for US Pacific "
            "in summer). When supplied, [start_date, end_date] are treated as the "
            "caller's local calendar days and converted to the matching UTC "
            "instant range, so sessions that end in the evening local time aren't "
            "misbucketed into the next UTC day. Omitted: falls back to comparing "
            "the UTC calendar date of ended_at."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return total time (in seconds) spent per note, for closed sessions ended within [start_date, end_date]."""
    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    require_date_range(start, end)

    if utc_offset_minutes is not None:
        # ended_at is a naive UTC wall-clock timestamp. Convert the caller's
        # local [start 00:00, end+1day 00:00) window into the matching UTC
        # instants — same offset convention as debrief's _local_day_bounds_utc.
        offset = timedelta(minutes=utc_offset_minutes)
        start_utc = datetime.combine(start, time_type.min) + offset
        end_utc = datetime.combine(end, time_type.min) + timedelta(days=1) + offset
        rows = get_time_by_note_for_datetime_range(db, current_user.id, start_utc, end_utc)
    else:
        rows = get_time_by_note_for_range(db, current_user.id, start, end)

    return [
        {"note_id": note_id, "total_seconds": total_seconds}
        for note_id, total_seconds in rows
    ]


@router.post("/note-session/reap", response_model=NoteSessionReapResult, status_code=status.HTTP_200_OK)
def reap_stale_note_sessions(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Force-close the user's own sessions left open too long (crashed tab, force-quit, etc)."""
    return {"reaped_count": reap_stale_sessions(db, current_user.id)}
