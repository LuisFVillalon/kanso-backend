"""
Event series: create, edit, archive and delete, one occurrence at a time or
many.

A change to a repeating event takes a scope, like Google Calendar:

- "this": only the occurrence the rule puts on occurrence_date. Its
  exception row holds the change.
- "following": that occurrence and every later one. The series is split:
  it now ends the day before, and a copy starting on occurrence_date (with
  split_from_id pointing back) takes the later exceptions and the change.
- "all": the series itself. Occurrences edited on their own keep the
  fields they changed.

A one-off event, and "following" from a series' first occurrence, act as
"all". Deleting moves the series, or the one occurrence, to the trash like
tasks and notes.
"""

from datetime import UTC, date, datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.crud.base import archived_filter, deleted_filter, get_owned, trash_cutoff
from app.crud.event_occurrence_crud import DAY, occurs_on, rule_dates
from app.crud.tag_crud import set_item_tags
from app.crud.trash_crud import move_to_trash, purge_expired, purge_from_trash, restore_from_trash
from app.models.event_model import Event, EventException
from app.schemas.event_schema import EditScope, EventCreate, EventUpdate, check_series

# The series' own fields, which "following" and "all" edits change.
SERIES_FIELDS = (
    "title", "description", "all_day", "start_date", "end_date", "start_time", "end_time",
    "time_zone", "repeat_days", "repeat_interval", "reminder_minutes",
)
# The fields one occurrence can override.
OCCURRENCE_FIELDS = {"title", "description", "tags", "all_day", "start_time", "end_time", "date"}


def get_events(db: Session, user_id: str, archived: bool = False) -> list[Event]:
    """The user's active series, or only their archived ones when archived=True. Deleted ones are left out."""
    return (
        db.query(Event)
        .options(selectinload(Event.tags))
        .filter(Event.user_id == user_id, deleted_filter(Event, False), archived_filter(Event, archived))
        .order_by(Event.start_date, Event.start_time.asc().nulls_first(), Event.id)
        .all()
    )


def get_event(db: Session, event_id: int, user_id: str) -> Event | None:
    return get_owned(db, Event, event_id, user_id)


def create_event(db: Session, body: EventCreate, user_id: str) -> Event:
    event = Event(user_id=user_id, **body.model_dump(exclude={"tags"}))
    set_item_tags(db, event, body.tags, user_id)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


# ── Scopes ───────────────────────────────────────────────────────────────────

def _resolve_scope(event: Event, scope: EditScope, occurrence_date: date | None) -> EditScope:
    """The scope a change really has, after checking the occurrence it names exists."""
    if not event.repeat_days or scope == "all":
        return "all"
    if occurrence_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"occurrence_date is required with scope={scope}."
        )
    if not occurs_on(event, occurrence_date):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"The event has no occurrence on {occurrence_date}."
        )
    if scope == "following" and next(rule_dates(event, event.start_date, occurrence_date - DAY), None) is None:
        return "all"
    return scope


def _split(db: Session, event: Event, occurrence_date: date) -> Event:
    """End the series the day before occurrence_date and return a copy that carries on from it."""
    rest = Event(
        user_id=event.user_id,
        **{field: getattr(event, field) for field in SERIES_FIELDS},
        split_from_id=event.id,
        archived_at=event.archived_at,
    )
    rest.start_date = occurrence_date
    rest.tags = list(event.tags)
    for exc in [exc for exc in event.exceptions if exc.occurrence_date >= occurrence_date]:
        exc.event = rest
    event.end_date = occurrence_date - DAY
    db.add(rest)
    db.flush()
    return rest


def _exception(event: Event, occurrence_date: date) -> EventException:
    """The occurrence's exception row, added if it has none yet."""
    for exc in event.exceptions:
        if exc.occurrence_date == occurrence_date:
            return exc
    exc = EventException(occurrence_date=occurrence_date, tags_overridden=False, cancelled=False)
    event.exceptions.append(exc)
    return exc


def _drop_if_plain(db: Session, event: Event, exc: EventException) -> None:
    """Remove an exception that no longer changes anything about its occurrence."""
    overrides = ("date", "title", "description", "all_day", "start_time", "end_time", "archived_at", "deleted_at")
    if not exc.tags_overridden and not exc.cancelled and all(getattr(exc, f) is None for f in overrides):
        event.exceptions.remove(exc)
        db.delete(exc)


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


# ── Editing ──────────────────────────────────────────────────────────────────

def _edit_series(db: Session, event: Event, body: EventUpdate, changes: dict, user_id: str) -> None:
    values = {field: getattr(event, field) for field in SERIES_FIELDS}
    values.update({k: v for k, v in changes.items() if k in SERIES_FIELDS})
    try:
        values = check_series(values)
    except ValueError as e:
        raise _unprocessable(str(e)) from None
    for field, value in values.items():
        setattr(event, field, value)
    if "tags" in changes:
        set_item_tags(db, event, body.tags or [], user_id)


def _edit_occurrence(
    db: Session, event: Event, occurrence_date: date, body: EventUpdate, changes: dict, user_id: str
) -> None:
    exc = _exception(event, occurrence_date)
    for field in ("title", "all_day", "start_time", "end_time", "date"):
        if field in changes:
            setattr(exc, field, changes[field])
    if exc.date == occurrence_date:
        exc.date = None
    if "description" in changes:
        # Empty rather than NULL, which would bring back the series' description.
        exc.description = changes["description"] or ""
    if "tags" in changes:
        if not exc.tags_overridden:
            # Start from the series' tags, so keeping an archived one doesn't restore it.
            exc.tags = list(event.tags)
            exc.tags_overridden = True
        set_item_tags(db, exc, body.tags or [], user_id)

    all_day = event.all_day if exc.all_day is None else exc.all_day
    start_time = exc.start_time or event.start_time
    end_time = exc.end_time or event.end_time
    if not all_day:
        if start_time is None or end_time is None:
            raise _unprocessable("A timed event needs both start_time and end_time")
        if start_time == end_time:
            raise _unprocessable("An event must end at a different time than it starts")
    _drop_if_plain(db, event, exc)


def update_event(
    db: Session, event_id: int, body: EventUpdate, user_id: str, scope: EditScope, occurrence_date: date | None
) -> Event | None:
    """Apply the fields given to the occurrences in scope. Returns the series holding them, or None if not found."""
    event = get_owned(db, Event, event_id, user_id)
    if not event:
        return None
    changes = body.model_dump(exclude_unset=True)
    scope = _resolve_scope(event, scope, occurrence_date)
    if scope == "this":
        series_only = sorted(set(changes) - OCCURRENCE_FIELDS)
        if series_only:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{', '.join(series_only)} can't change for one occurrence; use scope=following or all.",
            )
        _edit_occurrence(db, event, occurrence_date, body, changes, user_id)
    else:
        if "date" in changes:
            if event.repeat_days:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="date moves one occurrence; use scope=this, or start_date to move the series.",
                )
            # A one-off event's only occurrence is the series.
            changes.setdefault("start_date", changes["date"])
        if scope == "following":
            event = _split(db, event, occurrence_date)
        _edit_series(db, event, body, changes, user_id)
    db.commit()
    db.refresh(event)
    return event


# ── Archive ──────────────────────────────────────────────────────────────────

def set_event_archived(
    db: Session, event_id: int, user_id: str, archived: bool, scope: EditScope, occurrence_date: date | None
) -> Event | None:
    """Archive or unarchive the occurrences in scope. Returns the series holding them, or None if not found.

    Idempotent, like set_archived. Unarchiving one occurrence clears only
    its own archive: an occurrence of an archived series stays archived
    until the series is unarchived.
    """
    event = get_owned(db, Event, event_id, user_id)
    if not event:
        return None
    scope = _resolve_scope(event, scope, occurrence_date)
    now = datetime.now(UTC)
    if scope == "this":
        exc = _exception(event, occurrence_date)
        exc.archived_at = (exc.archived_at or now) if archived else None
        _drop_if_plain(db, event, exc)
    elif (event.archived_at is not None) != archived:
        if scope == "following":
            event = _split(db, event, occurrence_date)
        event.archived_at = now if archived else None
    db.commit()
    db.refresh(event)
    return event


# ── Trash ────────────────────────────────────────────────────────────────────

def delete_event(
    db: Session, event_id: int, user_id: str, scope: EditScope, occurrence_date: date | None
) -> Event | None:
    """Move the occurrences in scope to the trash. Returns the series holding them, or None if not found.

    "all" trashes the series; "following" splits it and trashes the later
    part; "this" trashes the one occurrence, which the trash lists with its
    occurrence_date.
    """
    event = get_owned(db, Event, event_id, user_id)
    if not event:
        return None
    scope = _resolve_scope(event, scope, occurrence_date)
    if scope == "this":
        _exception(event, occurrence_date).deleted_at = datetime.now(UTC)
    else:
        if scope == "following":
            event = _split(db, event, occurrence_date)
            db.commit()
        return move_to_trash(db, Event, event.id, user_id)
    purge_expired(db)
    db.commit()
    db.refresh(event)
    return event


def _trashed_occurrence(db: Session, event_id: int, user_id: str, occurrence_date: date) -> EventException | None:
    """A deleted occurrence of a live series, still in the trash."""
    purge_expired(db)
    db.commit()
    return (
        db.query(EventException)
        .join(Event, Event.id == EventException.event_id)
        .filter(
            Event.id == event_id,
            Event.user_id == user_id,
            Event.deleted_at.is_(None),
            EventException.occurrence_date == occurrence_date,
            EventException.deleted_at > trash_cutoff(),
        )
        .first()
    )


def restore_event(db: Session, event_id: int, user_id: str, occurrence_date: date | None = None) -> Event | None:
    """Bring a series, or one occurrence of it, back from the trash. Returns the series, or None if not there."""
    if occurrence_date is None:
        return restore_from_trash(db, Event, event_id, user_id)
    exc = _trashed_occurrence(db, event_id, user_id, occurrence_date)
    if not exc:
        return None
    event = exc.event
    exc.deleted_at = None
    _drop_if_plain(db, event, exc)
    db.commit()
    db.refresh(event)
    return event


def purge_event(db: Session, event_id: int, user_id: str, occurrence_date: date | None = None) -> Event | None:
    """Delete a series, or one occurrence, in the trash for good. Returns the series, or None if not there."""
    if occurrence_date is None:
        return purge_from_trash(db, Event, event_id, user_id)
    exc = _trashed_occurrence(db, event_id, user_id, occurrence_date)
    if not exc:
        return None
    event = exc.event
    exc.deleted_at = None
    exc.cancelled = True
    db.commit()
    db.refresh(event)
    return event
