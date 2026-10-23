from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.models.archived_term_model import ArchivedTerm
from app.models.calendar_settings_model import CalendarSettings as CalendarSettingsModel
from app.schemas.calendar_schema import ArchivedTerm as ArchivedTermOut


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
    archived = ArchivedTerm(
        user_id=user_id, title=current.title, start_date=current.start_date, end_date=current.end_date
    )
    db.add(archived)
    db.delete(current)
    db.commit()
    db.refresh(archived)
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
