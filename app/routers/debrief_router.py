from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_time_zone
from app.crud.debrief_crud import build_daily_debrief
from app.database.database import get_db
from app.schemas.debrief_schema import DailyDebriefReport

router = APIRouter()


@router.get("/daily-debrief", response_model=DailyDebriefReport)
def read_daily_debrief(
    local_date: str | None = Query(
        None,
        description="Caller's local calendar date (YYYY-MM-DD). Used instead of "
        "the server's own date so a server/user timezone mismatch can't roll "
        "'today' over early and misclassify today's tasks as overdue.",
    ),
    local_time: str | None = Query(
        None,
        description="Caller's local clock time (HH:MM). Used to tell whether a "
        "same-day due_time has already passed.",
    ),
    tz: str | None = Query(
        None,
        description="Caller's IANA time zone, e.g. America/Los_Angeles, which today's events are "
        "placed in. Without it each event is read in its own time zone.",
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's daily debrief: overdue/due-today tasks,
    habit status, today's events, workload capacity, and unified focus-next
    recommendations."""
    return build_daily_debrief(
        db,
        current_user.id,
        local_date=local_date,
        local_time=local_time,
        tz=require_time_zone(tz) if tz else None,
    )
