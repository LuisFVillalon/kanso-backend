from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_date_range, require_found, require_time_zone
from app.crud.event_crud import (
    create_event,
    delete_event,
    get_event,
    get_events,
    purge_event,
    restore_event,
    set_event_archived,
    update_event,
)
from app.crud.event_occurrence_crud import get_archived_occurrences, get_event_report
from app.database.database import get_db
from app.routers.calendar_router import ACTIVITY_RANGE_MAX_DAYS
from app.schemas.event_schema import EditScope, Event, EventCreate, EventOccurrence, EventReport, EventUpdate

router = APIRouter()

_SCOPE = Query(
    "all",
    description="Which occurrences of a repeating event to change: this one (occurrence_date), "
    "it and every later one (following), or all of them. A one-off event ignores it.",
)
_OCCURRENCE_DATE = Query(
    None,
    description="The occurrence's occurrence_date (the day the series' rule puts it on), "
    "required with scope=this or following.",
)
_TRASHED_OCCURRENCE_DATE = Query(
    None, description="The trash item's occurrence_date, to act on one deleted occurrence instead of the series."
)
_TZ = Query(..., description="The caller's IANA time zone, e.g. America/Los_Angeles, which occurrences are placed in.")
_START = Query(..., description="First local calendar day of the range (YYYY-MM-DD).")
_END = Query(..., description="Last local calendar day of the range, inclusive (YYYY-MM-DD).")


@router.get("/get-events", response_model=list[Event])
def read_events(
    archived: bool = Query(False, description="Return only archived series instead of active ones."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's active event series, or their archived ones with ?archived=true."""
    return get_events(db, current_user.id, archived)


@router.get("/get-event/{event_id}", response_model=Event)
def read_event(
    event_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return require_found(get_event(db, event_id, current_user.id), "Event not found")


@router.get("/get-archived-event-occurrences", response_model=list[EventOccurrence])
def read_archived_event_occurrences(
    tz: str = _TZ,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return occurrences archived on their own, for Archive → Events. Whole
    archived series are listed by /get-events?archived=true."""
    return get_archived_occurrences(db, current_user.id, require_time_zone(tz), datetime.now(UTC))


@router.get("/get-event-report", response_model=EventReport)
def read_event_report(
    start_date: date = _START,
    end_date: date = _END,
    tz: str = _TZ,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return event time over local days start_date..end_date for Reports → Events:
    by tag, day, week and month, and the free time events leave in the work windows.
    archived_count counts past archived occurrences; upcoming archived ones are left out."""
    require_date_range(start_date, end_date, ACTIVITY_RANGE_MAX_DAYS)
    return get_event_report(db, current_user.id, start_date, end_date, require_time_zone(tz), datetime.now(UTC))


@router.post("/create-event", response_model=Event)
def create_new_event(
    event: EventCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_event(db, event, current_user.id)


@router.put("/update-event/{event_id}", response_model=Event)
def update_event_by_id(
    event_id: int,
    changes: EventUpdate,
    scope: EditScope = _SCOPE,
    occurrence_date: date | None = _OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the fields given on the occurrences in scope; fields left out keep their value.
    Returns the series that holds them: for scope=following, the new series split off."""
    return require_found(
        update_event(db, event_id, changes, current_user.id, scope, occurrence_date), "Event not found"
    )


@router.patch("/archive-event/{event_id}", response_model=Event)
def archive_event_by_id(
    event_id: int,
    scope: EditScope = _SCOPE,
    occurrence_date: date | None = _OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive the occurrences in scope. Past ones stay on the calendar, flagged; upcoming ones leave it."""
    return require_found(
        set_event_archived(db, event_id, current_user.id, True, scope, occurrence_date), "Event not found"
    )


@router.patch("/unarchive-event/{event_id}", response_model=Event)
def unarchive_event_by_id(
    event_id: int,
    scope: EditScope = _SCOPE,
    occurrence_date: date | None = _OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unarchive the occurrences in scope. scope=this clears only that occurrence's own archive."""
    return require_found(
        set_event_archived(db, event_id, current_user.id, False, scope, occurrence_date), "Event not found"
    )


@router.delete("/del-event/{event_id}", response_model=Event)
def delete_event_by_id(
    event_id: int,
    scope: EditScope = _SCOPE,
    occurrence_date: date | None = _OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move the occurrences in scope to the trash, restorable for 30 days.
    Returns the series that holds them: for scope=following, the part split off and trashed."""
    return require_found(delete_event(db, event_id, current_user.id, scope, occurrence_date), "Event not found")


@router.patch("/restore-event/{event_id}", response_model=Event)
def restore_event_by_id(
    event_id: int,
    occurrence_date: date | None = _TRASHED_OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bring a series, or one occurrence, back from the trash, as it was."""
    return require_found(
        restore_event(db, event_id, current_user.id, occurrence_date), "Event not found in the trash"
    )


@router.delete("/purge-event/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def purge_event_by_id(
    event_id: int,
    occurrence_date: date | None = _TRASHED_OCCURRENCE_DATE,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a series, or one occurrence, that is in the trash, without waiting out the 30 days."""
    require_found(purge_event(db, event_id, current_user.id, occurrence_date), "Event not found in the trash")
