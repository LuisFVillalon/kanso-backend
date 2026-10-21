from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_date_range, require_found
from app.crud.mood_crud import delete_checkin, get_checkins, get_mood_tags, save_checkin
from app.database.database import get_db
from app.schemas.mood_schema import MOOD_PERIOD_KINDS, MoodCheckin, MoodCheckinSave, MoodTags

router = APIRouter()

# Long enough for a leap year's report, short enough to bound one response.
MOOD_RANGE_MAX_DAYS = 366


def _parse_kinds(kinds: str | None) -> list[str]:
    if kinds is None:
        return list(MOOD_PERIOD_KINDS)
    parsed = [k.strip() for k in kinds.split(",") if k.strip()]
    unknown = [k for k in parsed if k not in MOOD_PERIOD_KINDS]
    if not parsed or unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"kinds must be a comma-separated list of {', '.join(MOOD_PERIOD_KINDS)}.",
        )
    return parsed


@router.get("/get-mood-checkins", response_model=list[MoodCheckin])
def read_mood_checkins(
    start_date: date = Query(..., description="First local calendar day of the range (YYYY-MM-DD)."),
    end_date: date = Query(..., description="Last local calendar day of the range, inclusive (YYYY-MM-DD)."),
    kinds: str | None = Query(
        None, description="Comma-separated period kinds to include, e.g. day,week. Default: day,week,month."
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's check-ins whose period_start falls in [start_date, end_date], oldest first."""
    require_date_range(start_date, end_date, MOOD_RANGE_MAX_DAYS)
    return get_checkins(db, current_user.id, start_date, end_date, _parse_kinds(kinds))


@router.put("/save-mood-checkin", response_model=MoodCheckin)
def save_mood_checkin(
    body: MoodCheckinSave,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or replace the check-in for body's period_kind and period_start."""
    return save_checkin(db, current_user.id, body)


@router.delete("/del-mood-checkin/{checkin_id}", response_model=MoodCheckin)
def delete_mood_checkin(
    checkin_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a check-in for good, returning it so the client can offer Undo by saving it again."""
    return require_found(delete_checkin(db, checkin_id, current_user.id), "Mood check-in not found")


@router.get("/get-mood-tags", response_model=MoodTags)
def read_mood_tags(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the default mood tags and the custom ones the user has used. These are separate from task tags."""
    return get_mood_tags(db, current_user.id)
