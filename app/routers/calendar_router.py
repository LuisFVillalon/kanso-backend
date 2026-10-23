from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_date_range, require_found, require_time_zone
from app.crud.activity_crud import get_calendar_activity
from app.crud.calendar_crud import (
    archive_current_term,
    delete_archived_term,
    get_calendar_settings,
    list_archived_terms,
    upsert_calendar_settings,
)
from app.database.database import get_db
from app.schemas.activity_schema import CalendarActivity
from app.schemas.calendar_schema import ArchivedTerm, CalendarSettings, CalendarSettingsUpdate

router = APIRouter()

# Long enough for a leap year's report, short enough to bound one response.
ACTIVITY_RANGE_MAX_DAYS = 366


@router.get("/get-calendar-settings", response_model=CalendarSettings)
def read_calendar_settings(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's archived terms, earliest first, so the month calendar can show past terms."""
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
            "Deleted (trashed) items are always left out."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's planned and completed tasks, note sessions and habit
    completions across [start_date, end_date], for the calendar week view and
    weekly/monthly/yearly reports (which ask for archived items too)."""
    require_date_range(start_date, end_date, ACTIVITY_RANGE_MAX_DAYS)
    return get_calendar_activity(db, current_user.id, start_date, end_date, require_time_zone(tz), include_archived)
