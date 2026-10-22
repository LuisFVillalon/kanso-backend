from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import active_filter, deleted_filter, get_owned
from app.models.note_model import Note as NoteModel
from app.models.note_session_model import NoteSession as NoteSessionModel

# A session still open past this age is treated as orphaned (crashed tab,
# force-quit) rather than a genuine sitting — visibilitychange/pagehide on
# the frontend already close a real session well before this on any normal
# backgrounding or tab close. See reap_stale_sessions.
STALE_SESSION_HOURS = 4


def start_session(db: Session, note_id: int, user_id: str) -> NoteSessionModel | None:
    """Open a new editing session for a note the user owns. Returns None if the note doesn't exist / isn't owned."""
    note = get_owned(db, NoteModel, note_id, user_id)
    if not note:
        return None
    session = NoteSessionModel(note_id=note_id, user_id=user_id, started_at=datetime.now(UTC))
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def end_session(db: Session, session_id: int, user_id: str) -> NoteSessionModel | None:
    """Close an open session. Returns None if not found, not owned, or already ended."""
    session = get_owned(db, NoteSessionModel, session_id, user_id)
    if not session or session.ended_at is not None:
        return None
    session.ended_at = datetime.now(UTC)
    db.commit()
    db.refresh(session)
    return session


def get_total_time_seconds(db: Session, note_id: int, user_id: str) -> int:
    """Sum the duration of all closed sessions for a note. Open (never-ended) sessions aren't counted.

    A deleted note counts as gone, so its total is 0 until it's restored.
    """
    total = (
        db.query(
            func.coalesce(
                func.sum(func.extract("epoch", NoteSessionModel.ended_at - NoteSessionModel.started_at)),
                0,
            )
        )
        .join(NoteModel, NoteModel.id == NoteSessionModel.note_id)
        .filter(
            NoteSessionModel.note_id == note_id,
            NoteSessionModel.user_id == user_id,
            NoteSessionModel.ended_at.isnot(None),
            deleted_filter(NoteModel, False),
        )
        .scalar()
    )
    return int(total or 0)


def _closed_seconds_by_active_note(db: Session, user_id: str, *conditions) -> list[tuple[int, int]]:
    """Sum closed-session duration per note for the user, for sessions matching conditions.

    Archived and deleted notes are left out: their time no longer counts toward totals or stats.
    """
    rows = (
        db.query(
            NoteSessionModel.note_id,
            func.sum(func.extract("epoch", NoteSessionModel.ended_at - NoteSessionModel.started_at)),
        )
        .join(NoteModel, NoteModel.id == NoteSessionModel.note_id)
        .filter(
            NoteSessionModel.user_id == user_id,
            NoteSessionModel.ended_at.isnot(None),
            active_filter(NoteModel),
            *conditions,
        )
        .group_by(NoteSessionModel.note_id)
        .all()
    )
    return [(note_id, int(total or 0)) for note_id, total in rows]


def get_total_time_by_note(db: Session, user_id: str) -> list[tuple[int, int]]:
    """Sum closed-session duration per active note for the user. Notes with zero closed sessions are omitted."""
    return _closed_seconds_by_active_note(db, user_id)


def get_time_by_note_for_date(db: Session, user_id: str, target_date: date) -> list[tuple[int, int]]:
    """Sum closed-session duration per active note for the user, for sessions that ended on target_date.

    Compares the UTC calendar date of ended_at, with no timezone offset
    applied. This is a fallback for callers that can't supply the caller's
    local time — e.g. the debrief when local_time wasn't passed. When an
    offset is available, prefer get_time_by_note_for_datetime_range, which
    compares against explicit UTC instant bounds instead of a UTC calendar
    date and so doesn't misclassify sessions that end near midnight in the
    caller's timezone.
    """
    return _closed_seconds_by_active_note(db, user_id, func.date(NoteSessionModel.ended_at) == target_date)


def get_time_by_note_for_datetime_range(
    db: Session, user_id: str, start: datetime, end: datetime
) -> list[tuple[int, int]]:
    """Sum closed-session duration per active note for the user, for sessions with ended_at in [start, end).

    Unlike get_time_by_note_for_date, start/end are explicit UTC instants
    rather than a UTC calendar date, so a caller that has derived the UTC
    range corresponding to its own local calendar day (see
    debrief_crud._local_day_bounds_utc) gets sessions bucketed by that local
    day instead of the UTC day.
    """
    return _closed_seconds_by_active_note(
        db, user_id, NoteSessionModel.ended_at >= start, NoteSessionModel.ended_at < end
    )


def get_time_by_note_for_range(db: Session, user_id: str, start_date: date, end_date: date) -> list[tuple[int, int]]:
    """Sum closed-session duration per active note for the user, for sessions ended within [start_date, end_date].

    Compares the UTC calendar date of ended_at against the given bounds — the
    same day-boundary approximation get_time_by_note_for_date already makes,
    extended to an inclusive range instead of a single day.
    """
    return _closed_seconds_by_active_note(
        db,
        user_id,
        func.date(NoteSessionModel.ended_at) >= start_date,
        func.date(NoteSessionModel.ended_at) <= end_date,
    )


def get_time_by_note_for_local_days(
    db: Session, user_id: str, start: date, end: date, utc_offset_minutes: int | None
) -> list[tuple[int, int]]:
    """Sum closed-session duration per active note for the user, for sessions ended within local days start..end.

    utc_offset_minutes is the caller's offset as JS Date.getTimezoneOffset()
    gives it (minutes to add to local time to reach UTC). With it, the
    caller's local [start 00:00, end+1day 00:00) window becomes the matching
    UTC instants (same offset convention as debrief_crud._local_day_bounds_utc);
    without it, this falls back to get_time_by_note_for_range's UTC dates.
    """
    if utc_offset_minutes is None:
        return get_time_by_note_for_range(db, user_id, start, end)
    # ended_at is a naive UTC wall-clock timestamp.
    offset = timedelta(minutes=utc_offset_minutes)
    start_utc = datetime.combine(start, time.min) + offset
    end_utc = datetime.combine(end, time.min) + timedelta(days=1) + offset
    return get_time_by_note_for_datetime_range(db, user_id, start_utc, end_utc)


def reap_stale_sessions(db: Session, user_id: str) -> int:
    """Force-close the user's sessions left open longer than STALE_SESSION_HOURS.

    Closed at their own started_at (a zero-duration close) rather than now() —
    an orphaned session could have sat open for hours or days, and crediting
    that whole span to the note would wildly inflate its total. Zero-duration
    just stops it lingering as "open" forever without adding fake time.

    Returns the number of sessions closed.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=STALE_SESSION_HOURS)
    stale_sessions = (
        db.query(NoteSessionModel)
        .filter(
            NoteSessionModel.user_id == user_id,
            NoteSessionModel.ended_at.is_(None),
            NoteSessionModel.started_at < cutoff,
        )
        .all()
    )
    for session in stale_sessions:
        session.ended_at = session.started_at
    db.commit()
    return len(stale_sessions)
