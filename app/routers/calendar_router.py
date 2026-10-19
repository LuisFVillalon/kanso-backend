from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.activity_crud import get_calendar_activity
from app.crud.calendar_crud import get_calendar_settings, upsert_calendar_settings
from app.database.database import get_db
from app.schemas.activity_schema import CalendarActivity
from app.schemas.calendar_schema import CalendarSettings, CalendarSettingsUpdate

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


@router.get("/get-calendar-activity", response_model=CalendarActivity)
def read_calendar_activity(
    start_date: date = Query(..., description="First local calendar day of the range (YYYY-MM-DD)."),
    end_date: date = Query(..., description="Last local calendar day of the range, inclusive (YYYY-MM-DD)."),
    utc_offset_minutes: int = Query(
        0,
        ge=-14 * 60,
        le=14 * 60,
        description=(
            "The caller's UTC offset as returned by JS Date.getTimezoneOffset() "
            "(minutes to add to local time to reach UTC; e.g. 420 for US Pacific "
            "in summer). Task completions and note sessions are matched against "
            "the UTC instants the caller's local days cover. Omitted: UTC days."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's planned and completed tasks, note sessions and habit
    completions across [start_date, end_date], for the calendar week view and
    weekly/monthly/yearly reports."""
    if start_date > end_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start_date must not be after end_date.")
    if (end_date - start_date).days + 1 > ACTIVITY_RANGE_MAX_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The range may span at most {ACTIVITY_RANGE_MAX_DAYS} days.",
        )
    return get_calendar_activity(db, current_user.id, start_date, end_date, utc_offset_minutes)
