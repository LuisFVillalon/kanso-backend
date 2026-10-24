from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_date_range, require_found, require_time_zone
from app.crud.activity_crud import get_calendar_activity
from app.crud.calendar_crud import (
    archive_current_term,
    archive_ended_term,
    delete_archived_term,
    delete_next_term,
    dismiss_ended_term,
    get_calendar_settings,
    get_term_rollover,
    list_archived_terms,
    roll_over_term,
    save_next_term,
    update_term_rollover,
    upsert_calendar_settings,
)
from app.crud.habit_crud import resolve_today
from app.database.database import get_db
from app.schemas.activity_schema import CalendarActivity
from app.schemas.calendar_schema import (
    ArchivedTerm,
    CalendarSettings,
    CalendarSettingsUpdate,
    NextTermSave,
    TermRollover,
    TermRolloverUpdate,
)

router = APIRouter()

# Long enough for a leap year's report, short enough to bound one response.
ACTIVITY_RANGE_MAX_DAYS = 366

LocalDate = Query(
    None,
    description="Caller's local calendar date (YYYY-MM-DD), the day a term's end is checked "
    "against when rolling it over; the server's date if omitted.",
)


@router.get("/get-calendar-settings", response_model=CalendarSettings)
def read_calendar_settings(
    local_date: str | None = LocalDate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current term, after rolling it over if it has ended."""
    roll_over_term(db, current_user.id, resolve_today(local_date))
    return require_found(get_calendar_settings(db, current_user.id), "No calendar settings found")


@router.patch("/update-calendar-settings", response_model=CalendarSettings)
def update_calendar_settings(
    changes: CalendarSettingsUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return upsert_calendar_settings(db, current_user.id, changes.model_dump(exclude_none=True))


@router.get("/get-archived-terms", response_model=list[ArchivedTerm])
def read_archived_terms(
    local_date: str | None = LocalDate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's archived terms, earliest first, so the month calendar can show past terms."""
    roll_over_term(db, current_user.id, resolve_today(local_date))
    return list_archived_terms(db, current_user.id)


@router.post("/archive-term", response_model=ArchivedTerm)
def archive_term(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive the current term tracker, freeing the user to set up a new one."""
    return require_found(archive_current_term(db, current_user.id), "No calendar settings found")


@router.delete("/del-archived-term/{term_id}", response_model=ArchivedTerm)
def delete_archived_term_by_id(
    term_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an archived term for good, returning it."""
    return require_found(delete_archived_term(db, term_id, current_user.id), "Archived term not found")


@router.get("/get-term-rollover", response_model=TermRollover)
def read_term_rollover(
    local_date: str | None = LocalDate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Auto-archive, the queued next term and any ended term left to archive, after rolling over."""
    roll_over_term(db, current_user.id, resolve_today(local_date))
    return get_term_rollover(db, current_user.id)


@router.patch("/update-term-rollover", response_model=TermRollover)
def update_rollover(
    changes: TermRolloverUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Turn auto-archive on or off; it applies from the next rollover."""
    return update_term_rollover(db, current_user.id, changes.auto_archive)


@router.put("/save-next-term", response_model=TermRollover)
def put_next_term(
    term: NextTermSave,
    local_date: str | None = LocalDate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Queue or replace the term that becomes current once the current term ends."""
    roll_over_term(db, current_user.id, resolve_today(local_date))
    return save_next_term(db, current_user.id, term)


@router.delete("/del-next-term", response_model=TermRollover)
def remove_next_term(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear the queued next term."""
    return require_found(delete_next_term(db, current_user.id), "No next term queued")


@router.post("/archive-ended-term", response_model=ArchivedTerm)
def archive_ended(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive the ended term a rollover kept because auto-archive was off."""
    return require_found(archive_ended_term(db, current_user.id), "No ended term to archive")


@router.delete("/del-ended-term", response_model=TermRollover)
def dismiss_ended(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Drop the ended term a rollover kept, without archiving it."""
    return require_found(dismiss_ended_term(db, current_user.id), "No ended term to archive")


@router.get("/get-calendar-activity", response_model=CalendarActivity)
def read_calendar_activity(
    start_date: date = Query(..., description="First local calendar day of the range (YYYY-MM-DD)."),
    end_date: date = Query(..., description="Last local calendar day of the range, inclusive (YYYY-MM-DD)."),
    tz: str = Query(
        ...,
        description=(
            "The caller's IANA time zone, e.g. America/Los_Angeles (the browser's "
            "Intl.DateTimeFormat().resolvedOptions().timeZone). Task completions and "
            "note sessions are matched against the UTC instants the caller's local days cover."
        ),
    ),
    include_archived: bool = Query(
        False,
        description=(
            "Also include archived tasks, archived notes' sessions and archived habits' logs. "
            "Deleted (trashed) items are always left out. Events ignore it: past archived "
            "occurrences are always in, upcoming archived ones always out."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's planned and completed tasks, note sessions, habit
    completions and event occurrences across [start_date, end_date], for the
    calendar week view and weekly/monthly/yearly reports (which ask for
    archived items too)."""
    require_date_range(start_date, end_date, ACTIVITY_RANGE_MAX_DAYS)
    return get_calendar_activity(db, current_user.id, start_date, end_date, require_time_zone(tz), include_archived)
