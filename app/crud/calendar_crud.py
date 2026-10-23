from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.models.archived_term_model import ArchivedTerm
from app.models.calendar_settings_model import CalendarSettings as CalendarSettingsModel
from app.models.term_rollover_model import TermRollover
from app.schemas.calendar_schema import ArchivedTerm as ArchivedTermOut
from app.schemas.calendar_schema import NextTermSave, TermDates
from app.schemas.calendar_schema import TermRollover as TermRolloverOut


def get_calendar_settings(db: Session, user_id: str) -> CalendarSettingsModel | None:
    return (
        db.query(CalendarSettingsModel)
        .filter(CalendarSettingsModel.user_id == user_id)
        .first()
    )


def _default_term() -> tuple[date, date]:
    """The current month plus the next two — mirrors defaultTermSettings() on the frontend."""
    today = date.today()
    start = today.replace(day=1)
    month_after = start.month + 3
    year = start.year + (month_after - 1) // 12
    end = date(year, (month_after - 1) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def upsert_calendar_settings(
    db: Session, user_id: str, changes: dict
) -> CalendarSettingsModel:
    row = get_calendar_settings(db, user_id)
    if row is None:
        default_start, default_end = _default_term()
        row = CalendarSettingsModel(
            user_id=user_id,
            title=changes.get("title", "Term Tracker"),
            start_date=changes.get("start_date", default_start),
            end_date=changes.get("end_date", default_end),
        )
        db.add(row)
    else:
        for key, val in changes.items():
            setattr(row, key, val)
    db.commit()
    db.refresh(row)
    return row


def list_archived_terms(db: Session, user_id: str) -> list[ArchivedTerm]:
    """The user's archived terms, earliest first."""
    return (
        db.query(ArchivedTerm)
        .filter(ArchivedTerm.user_id == user_id)
        .order_by(ArchivedTerm.start_date, ArchivedTerm.id)
        .all()
    )


def archive_current_term(db: Session, user_id: str) -> ArchivedTerm | None:
    """Move the user's current term into their archived terms. Returns None if they have no current term.

    The current term is removed, so the user can set up a new one; until they
    do, GET /get-calendar-settings answers 404, as for a user who never set one.
    """
    current = get_calendar_settings(db, user_id)
    if current is None:
        return None
    archived = _archive(db, user_id, current.title, current.start_date, current.end_date)
    db.delete(current)
    db.commit()
    db.refresh(archived)
    return archived


def _archive(db: Session, user_id: str, title: str, start_date: date, end_date: date) -> ArchivedTerm:
    archived = ArchivedTerm(user_id=user_id, title=title, start_date=start_date, end_date=end_date)
    db.add(archived)
    return archived


def delete_archived_term(db: Session, term_id: int, user_id: str) -> ArchivedTermOut | None:
    """Delete one of the user's archived terms. Returns what it held, or None if not found."""
    row = get_owned(db, ArchivedTerm, term_id, user_id)
    if row is None:
        return None
    # Read it out first: a deleted row can't be loaded again after the commit.
    deleted = ArchivedTermOut.model_validate(row)
    db.delete(row)
    db.commit()
    return deleted


# ── Rollover: the queued next term and auto-archive ─────────────────────────

def _rollover_out(row: TermRollover | None) -> TermRolloverOut:
    if row is None:
        return TermRolloverOut()
    next_term = ended_term = None
    if row.next_start_date is not None:
        next_term = TermDates(title=row.next_title, start_date=row.next_start_date, end_date=row.next_end_date)
    if row.ended_start_date is not None:
        ended_term = TermDates(title=row.ended_title, start_date=row.ended_start_date, end_date=row.ended_end_date)
    return TermRolloverOut(auto_archive=row.auto_archive, next_term=next_term, ended_term=ended_term)


def _locked_rollover(db: Session, user_id: str) -> TermRollover | None:
    # Row-locked so concurrent reads roll a term over once: the second waits,
    # then sees the first one's result.
    return db.query(TermRollover).filter(TermRollover.user_id == user_id).with_for_update().first()


def _get_or_create_rollover(db: Session, user_id: str) -> TermRollover:
    # Insert-if-missing first, so two first saves can't both insert the row.
    db.execute(insert(TermRollover).values(user_id=user_id).on_conflict_do_nothing())
    return _locked_rollover(db, user_id)


def roll_over_term(db: Session, user_id: str, today: date) -> None:
    """Bring the user's term tracker up to date for their local `today`.

    Once the current term has ended (its end_date is before today), it is
    archived as POST /archive-term does if auto_archive is on, and otherwise
    stays the current term, as without a next term queued. A queued next term
    becomes the current one once the current term has ended or there is none
    (e.g. it was archived by hand); an ended term it replaces without being
    archived is kept as ended_term. Running it again changes nothing.
    """
    rollover = _locked_rollover(db, user_id)
    if rollover is None:
        return
    current = (
        db.query(CalendarSettingsModel)
        .filter(CalendarSettingsModel.user_id == user_id)
        .with_for_update()
        .first()
    )
    ended = current is not None and current.end_date < today
    has_next = rollover.next_start_date is not None
    if not (ended and (rollover.auto_archive or has_next)) and not (current is None and has_next):
        return

    if ended and rollover.auto_archive:
        _archive(db, user_id, current.title, current.start_date, current.end_date)
        db.delete(current)
        current = None
    if has_next:
        if current is None:
            current = CalendarSettingsModel(user_id=user_id)
            db.add(current)
        else:
            rollover.ended_title = current.title
            rollover.ended_start_date = current.start_date
            rollover.ended_end_date = current.end_date
        current.title = rollover.next_title
        current.start_date = rollover.next_start_date
        current.end_date = rollover.next_end_date
        rollover.next_title = rollover.next_start_date = rollover.next_end_date = None
    db.commit()


def get_term_rollover(db: Session, user_id: str) -> TermRolloverOut:
    return _rollover_out(db.query(TermRollover).filter(TermRollover.user_id == user_id).first())


def update_term_rollover(db: Session, user_id: str, auto_archive: bool) -> TermRolloverOut:
    row = _get_or_create_rollover(db, user_id)
    row.auto_archive = auto_archive
    db.commit()
    return _rollover_out(row)


def save_next_term(db: Session, user_id: str, term: NextTermSave) -> TermRolloverOut:
    """Queue (or replace) the term that follows the current one.

    409 without a current term (set that up with PATCH /update-calendar-settings
    instead); 422 unless it starts after the current term ends.
    """
    current = get_calendar_settings(db, user_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No current term to follow; set up the current term instead.",
        )
    if term.start_date <= current.end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The next term must start after the current term ends.",
        )
    row = _get_or_create_rollover(db, user_id)
    row.next_title, row.next_start_date, row.next_end_date = term.title, term.start_date, term.end_date
    db.commit()
    return _rollover_out(row)


def delete_next_term(db: Session, user_id: str) -> TermRolloverOut | None:
    """Clear the queued next term. Returns None if none was queued."""
    row = _locked_rollover(db, user_id)
    if row is None or row.next_start_date is None:
        return None
    row.next_title = row.next_start_date = row.next_end_date = None
    db.commit()
    return _rollover_out(row)


def archive_ended_term(db: Session, user_id: str) -> ArchivedTerm | None:
    """Archive the ended term a rollover kept. Returns None if there is none."""
    row = _locked_rollover(db, user_id)
    if row is None or row.ended_start_date is None:
        return None
    archived = _archive(db, user_id, row.ended_title, row.ended_start_date, row.ended_end_date)
    row.ended_title = row.ended_start_date = row.ended_end_date = None
    db.commit()
    db.refresh(archived)
    return archived


def dismiss_ended_term(db: Session, user_id: str) -> TermRolloverOut | None:
    """Drop the ended term a rollover kept, without archiving it. Returns None if there is none."""
    row = _locked_rollover(db, user_id)
    if row is None or row.ended_start_date is None:
        return None
    row.ended_title = row.ended_start_date = row.ended_end_date = None
    db.commit()
    return _rollover_out(row)
