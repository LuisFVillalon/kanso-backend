from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.habit_crud import (
    create_habit,
    delete_habit,
    get_habit_history,
    get_habits,
    toggle_habit_log,
    toggle_habit_log_date,
    update_habit,
    verify_and_reset_streaks,
)
from app.database.database import get_db
from app.schemas.habit_schema import HabitCreate, HabitHistoryEntry, HabitResponse, HabitToggleDateRequest

router = APIRouter()


_LOCAL_DATE_QUERY = Query(
    None,
    description="Caller's local calendar date (YYYY-MM-DD). Used instead of the "
    "server's own date so a server/user timezone mismatch can't mark a habit "
    "logged for the user's local day as not-done-today.",
)


@router.get("/get-habits", response_model=list[HabitResponse])
def read_habits(
    local_date: str | None = _LOCAL_DATE_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all habits belonging to the authenticated user, with today's completion state."""
    return get_habits(db, current_user.id, local_date=local_date)


@router.post("/create-habit", response_model=HabitResponse)
def create_new_habit(
    habit: HabitCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new habit for the authenticated user."""
    return create_habit(db, habit, current_user.id)


@router.put("/update-habit/{habit_id}", response_model=HabitResponse)
def update_habit_by_id(
    habit_id: int,
    payload: HabitCreate,
    local_date: str | None = _LOCAL_DATE_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a habit's title, color, and tags."""
    return require_found(update_habit(db, habit_id, payload, current_user.id, local_date=local_date), "Habit not found")


@router.delete("/del-habit/{habit_id}", response_model=HabitResponse)
def delete_habit_by_id(
    habit_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a habit and all its logs (CASCADE)."""
    return require_found(delete_habit(db, habit_id, current_user.id), "Habit not found")


@router.patch("/toggle-habit/{habit_id}", response_model=HabitResponse)
def toggle_habit_completion(
    habit_id: int,
    local_date: str | None = _LOCAL_DATE_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle today's completion for a habit. Updates current_streak and max_streak accordingly."""
    return require_found(toggle_habit_log(db, habit_id, current_user.id, local_date=local_date), "Habit not found")


@router.post("/verify-streaks", status_code=status.HTTP_200_OK)
def verify_habit_streaks(
    local_date: str | None = _LOCAL_DATE_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reset current_streak to 0 for any habits whose last log is older than yesterday."""
    return verify_and_reset_streaks(db, current_user.id, local_date=local_date)


@router.get("/habit-history/{habit_id}", response_model=list[HabitHistoryEntry])
def read_habit_history(
    habit_id: int,
    days: int = Query(30, ge=1, le=365),
    local_date: str | None = _LOCAL_DATE_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the logged/not-logged status for the past `days` days for a habit."""
    return require_found(
        get_habit_history(db, habit_id, current_user.id, days, local_date=local_date),
        "Habit not found",
    )


@router.patch("/toggle-habit-date/{habit_id}", response_model=HabitResponse)
def toggle_habit_completion_date(
    habit_id: int,
    payload: HabitToggleDateRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle completion for a specific past date (or today). Recalculates streaks from scratch."""
    try:
        target = date_type.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date format. Use YYYY-MM-DD."
        ) from None
    # Bound "future" by the caller's local date when supplied — the server's
    # own date.today() may already be tomorrow relative to the user.
    local_today = date_type.today()
    if payload.local_date:
        try:
            local_today = date_type.fromisoformat(payload.local_date)
        except ValueError:
            pass
    if target > max(local_today, date_type.today()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot log future dates.")
    return require_found(
        toggle_habit_log_date(db, habit_id, current_user.id, target, local_date=payload.local_date),
        "Habit not found",
    )
