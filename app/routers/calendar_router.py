from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.calendar_crud import get_calendar_settings, upsert_calendar_settings
from app.database.database import get_db
from app.schemas.calendar_schema import CalendarSettings, CalendarSettingsUpdate

router = APIRouter()


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
