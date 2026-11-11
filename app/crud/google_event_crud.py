"""
Kanso's own changes to events imported from Google Calendar: edits, tags,
archive and delete. Nothing here is ever written to Google.

An imported event is addressed by the negative event_id its occurrences
carry (-row.id), on the same event routes as a kanso event. A change takes
a scope like a kanso event's (crud/event_crud.py):

- "this": only that instance.
- "following": that instance of a repeating Google event and every later
  one, by where Google starts them, those later syncs import included.
- "all": every instance of it, those later syncs import included.

An event that doesn't repeat is one row, so every scope is "this" for it.
"following" and "all" change the rows already imported and store a
GoogleEventOverride. A sync applies every override that reaches a newly
imported instance, oldest first, so the latest change wins, and a later
change to one instance still overrides it on its own.

Latest change wins against Google too: a sync keeps an edited row's details
unless Google changed the event after the edit, and an edit override reaches
a new instance only if Google last changed it before the override. Tags,
archives and deletes are kanso's alone, so syncs keep them.

Archive and delete work as for kanso events. An archived event's past
instances stay on the calendar, flagged, and upcoming ones leave it. A
deleted one waits in the trash for TRASH_RETENTION, restorable, and is then
cancelled: hidden for good, its row kept so a sync doesn't bring it back.
Instances archived or deleted together by one "following" or "all" change
are listed as one item, under the earliest one's id, and unarchiving with
"following" or "all", restoring and purging act on them together.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.crud.base import TRASH_RETENTION, trash_cutoff
from app.crud.event_occurrence_crud import DAY, _instant, _midnight
from app.crud.tag_crud import get_or_create_tag
from app.models.google_calendar_model import GoogleCalendarEvent, GoogleEventOverride
from app.models.tag_model import Tag
from app.schemas.event_schema import EditScope, Event, EventUpdate
from app.schemas.google_calendar_schema import GoogleEventTagsOut
from app.schemas.trash_schema import TrashItem

# What one instance can change, as kanso events' scope=this can.
OCCURRENCE_FIELDS = {"title", "description", "tags", "all_day", "start_time", "end_time", "date"}
# What "following" and "all" change on each instance; each keeps its own day.
SERIES_EDIT_FIELDS = ("title", "description", "all_day", "start_time", "end_time", "time_zone")
_PLACEMENT_FIELDS = {"all_day", "start_time", "end_time", "time_zone", "date", "start_date"}
# What an event that doesn't repeat can change, as a one-off kanso event's
# series can; start_date moves it like date.
_ONE_OFF_FIELDS = (OCCURRENCE_FIELDS | _PLACEMENT_FIELDS) - {"tags"}
# end_date, repeat_days and repeat_interval are the repeat rule, which stays
# Google's; reminder_minutes isn't kept for imported events. All are ignored.


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


# ── Lookups ──────────────────────────────────────────────────────────────────

def get_row(db: Session, event_id: int, user_id: str, trashed: bool = False) -> GoogleCalendarEvent | None:
    """The user's imported event an occurrence's event_id names, or None.

    Deleted ones are left out, as if gone; trashed=True fetches only one
    still in the trash instead. Cancelled ones are always left out.
    """
    if event_id >= 0:
        return None
    query = db.query(GoogleCalendarEvent).filter(
        GoogleCalendarEvent.id == -event_id,
        GoogleCalendarEvent.user_id == user_id,
        GoogleCalendarEvent.cancelled.is_(False),
    )
    if trashed:
        query = query.filter(GoogleCalendarEvent.deleted_at > trash_cutoff())
    else:
        query = query.filter(GoogleCalendarEvent.deleted_at.is_(None))
    return query.first()


def _resolve(row: GoogleCalendarEvent, scope: EditScope) -> EditScope:
    return scope if row.recurring_event_id is not None else "this"


def _targets(db: Session, row: GoogleCalendarEvent, scope: EditScope) -> list[GoogleCalendarEvent]:
    """The instances a change with this (resolved) scope reaches, deleted ones included."""
    if scope == "this":
        return [row]
    query = (
        db.query(GoogleCalendarEvent)
        .options(selectinload(GoogleCalendarEvent.tags))
        .filter(
            GoogleCalendarEvent.user_id == row.user_id,
            GoogleCalendarEvent.recurring_event_id == row.recurring_event_id,
            GoogleCalendarEvent.cancelled.is_(False),
        )
    )
    if scope == "following":
        query = query.filter(GoogleCalendarEvent.google_starts_at >= row.google_starts_at)
    return query.order_by(GoogleCalendarEvent.google_starts_at, GoogleCalendarEvent.id).all()


def _override(db: Session, row: GoogleCalendarEvent, scope: EditScope, kind: str, now: datetime, **values):
    """Keep a "following" or "all" change for the instances later syncs import."""
    override = GoogleEventOverride(
        user_id=row.user_id,
        recurring_event_id=row.recurring_event_id,
        starts_from=row.google_starts_at if scope == "following" else None,
        kind=kind,
        set_at=now,
        **values,
    )
    db.add(override)
    db.flush()
    return override


def event_out(row: GoogleCalendarEvent) -> Event:
    """An imported event in the shape of a kanso event series."""
    zone = ZoneInfo(row.time_zone)
    return Event(
        id=-row.id,
        title=row.title,
        description=row.description,
        tags=row.resolved_tags,
        all_day=row.all_day,
        start_date=row.start_date,
        start_time=None if row.all_day else row.starts_at.astimezone(zone).time(),
        end_time=None if row.all_day else row.ends_at.astimezone(zone).time(),
        time_zone=row.time_zone,
        created_date=row.created_at,
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        source="google",
        repeating=row.recurring_event_id is not None,
    )


def get_event(db: Session, event_id: int, user_id: str) -> Event | None:
    row = get_row(db, event_id, user_id)
    return event_out(row) if row else None


# ── Editing ──────────────────────────────────────────────────────────────────

def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise _unprocessable(f"Unknown time zone: {name!r}") from None


def _place(row: GoogleCalendarEvent, changes: dict) -> None:
    """Move or retime the row as kanso events are: wall times in its time zone.

    A day or zone change alone keeps its length; new times end the same day,
    or the next if the end is before the start.
    """
    old_zone = ZoneInfo(row.time_zone)
    zone_name = changes.get("time_zone") or row.time_zone
    zone = _zone(zone_name)
    day = changes.get("date") or changes.get("start_date") or row.start_date
    all_day = row.all_day if changes.get("all_day") is None else changes["all_day"]
    if all_day:
        span = row.end_date - row.start_date if row.all_day else timedelta(0)
        row.start_date, row.end_date = day, day + span
        row.starts_at, row.ends_at = _midnight(day, zone), _midnight(day + span + DAY, zone)
    else:
        start_time = changes.get("start_time") or (None if row.all_day else row.starts_at.astimezone(old_zone).time())
        end_time = changes.get("end_time") or (None if row.all_day else row.ends_at.astimezone(old_zone).time())
        if start_time is None or end_time is None:
            raise _unprocessable("A timed event needs both start_time and end_time")
        starts_at = _instant(day, start_time, zone)
        if not row.all_day and not changes.get("start_time") and not changes.get("end_time"):
            ends_at = starts_at + (row.ends_at - row.starts_at)
        else:
            if start_time == end_time:
                raise _unprocessable("An event must end at a different time than it starts")
            ends_at = _instant(day + DAY if end_time <= start_time else day, end_time, zone)
        row.starts_at, row.ends_at = starts_at, ends_at
        row.start_date, row.end_date = starts_at.astimezone(zone).date(), ends_at.astimezone(zone).date()
    row.all_day = all_day
    row.time_zone = zone_name


def _edit_row(row: GoogleCalendarEvent, changes: dict, edited_at: datetime) -> None:
    """Apply the details given to one row, marking it edited in kanso."""
    if not changes:
        return
    if "title" in changes:
        row.title = changes["title"]
    if "description" in changes:
        row.description = changes["description"] or None
    if _PLACEMENT_FIELDS & set(changes):
        _place(row, changes)
    row.edited_at = edited_at


def _stored_changes(changes: dict) -> dict:
    return {k: v.isoformat() if isinstance(v, time) else v for k, v in changes.items()}


def _loaded_changes(changes: dict | None) -> dict:
    return {
        k: time.fromisoformat(v) if k in ("start_time", "end_time") and v else v
        for k, v in (changes or {}).items()
    }


def _named_tags(db: Session, row: GoogleCalendarEvent, body: EventUpdate, user_id: str) -> list[Tag]:
    """The tags an update names, found or made like set_item_tags does for kanso events."""
    current = {tag.id for tag in row.resolved_tags}
    tags = []
    for tag_data in body.tags or []:
        tag = get_or_create_tag(db, tag_data, user_id)
        if tag.deleted_at is not None or tag.id not in current:
            tag.archived_at = None
            tag.deleted_at = None
        if tag not in tags:
            tags.append(tag)
    return tags


def update_event(
    db: Session, event_id: int, body: EventUpdate, user_id: str, scope: EditScope
) -> Event | None:
    """Apply the fields given to the instances in scope, in kanso only. Returns the instance, or None."""
    row = get_row(db, event_id, user_id)
    if row is None:
        return None
    changes = body.model_dump(exclude_unset=True)
    tags_given = "tags" in changes
    scope = _resolve(row, scope)
    now = datetime.now(UTC)
    if row.recurring_event_id is None:
        # Its only instance is the event, as for a one-off kanso event.
        _edit_row(row, {k: v for k, v in changes.items() if k in _ONE_OFF_FIELDS}, now)
    elif scope == "this":
        series_only = sorted(set(changes) - OCCURRENCE_FIELDS)
        if series_only:
            raise _bad_request(
                f"{', '.join(series_only)} can't change for one occurrence; use scope=following or all."
            )
        _edit_row(row, {k: v for k, v in changes.items() if k != "tags"}, now)
    else:
        if "date" in changes:
            raise _bad_request("date moves one occurrence; use scope=this.")
        edits = {k: v for k, v in changes.items() if k in SERIES_EDIT_FIELDS}
        if edits:
            for target in _targets(db, row, scope):
                _edit_row(target, edits, now)
            _override(db, row, scope, "edit", now, changes=_stored_changes(edits))
    if tags_given:
        _apply_tags(db, row, scope, _named_tags(db, row, body, user_id), False, now)
    db.commit()
    db.refresh(row)
    return event_out(row)


# ── Tags ─────────────────────────────────────────────────────────────────────

def _set_row_tags(row: GoogleCalendarEvent, tags: list[Tag], use_default: bool) -> None:
    # Trashed tags stay linked, ready for the tag's restore, like set_item_tags.
    row.tags = [] if use_default else list(tags) + [t for t in row.tags if t.deleted_at is not None and t not in tags]
    row.tags_overridden = not use_default


def _apply_tags(
    db: Session, row: GoogleCalendarEvent, scope: EditScope, tags: list[Tag], use_default: bool, now: datetime
) -> None:
    scope = _resolve(row, scope)
    if scope != "this":
        override = _override(db, row, scope, "tags", now, use_default=use_default)
        override.tags = [] if use_default else list(tags)
    for target in _targets(db, row, scope):
        _set_row_tags(target, tags, use_default)


def _tag_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")


def set_event_tags(
    db: Session, event_id: int, user_id: str, tag_ids: list[int], use_default: bool, scope: EditScope = "this"
) -> GoogleEventTagsOut | None:
    """Replace the own tags of the instances in scope, by the event_id its occurrences carry.

    Returns None if that isn't one of the user's imported events.
    """
    row = get_row(db, event_id, user_id)
    if row is None:
        return None
    ids = set(tag_ids)
    tags = db.query(Tag).filter(Tag.id.in_(ids), Tag.user_id == user_id, Tag.deleted_at.is_(None)).all() if ids else []
    if len(tags) != len(ids):
        raise _tag_not_found()
    _apply_tags(db, row, scope, tags, use_default, datetime.now(UTC))
    db.commit()
    db.refresh(row)
    return GoogleEventTagsOut(
        event_id=event_id,
        tags=[tag for tag in row.resolved_tags if tag.deleted_at is None],
        use_default=use_default,
    )


# ── Archive ──────────────────────────────────────────────────────────────────

def set_archived(db: Session, event_id: int, user_id: str, archived: bool, scope: EditScope) -> Event | None:
    """Archive or unarchive the instances in scope. Returns the instance, or None.

    Like kanso events: idempotent, and unarchiving one instance clears only
    its own archive, so one archived with others stays archived until they
    are unarchived with "following" or "all".
    """
    row = get_row(db, event_id, user_id)
    if row is None:
        return None
    scope = _resolve(row, scope)
    now = datetime.now(UTC)
    if scope == "this":
        if archived and row.archived_at is None:
            row.archived_at = now
        elif not archived and row.archived_by_id is None:
            row.archived_at = None
    else:
        override = _override(db, row, scope, "archive" if archived else "unarchive", now)
        for target in _targets(db, row, scope):
            _archive_by(target, override, archived)
    db.commit()
    db.refresh(row)
    return event_out(row)


def _archive_by(row: GoogleCalendarEvent, override: GoogleEventOverride, archived: bool) -> None:
    if archived and row.archived_at is None:
        row.archived_at, row.archived_by_id = override.set_at, override.id
    elif not archived and row.archived_by_id is not None:
        row.archived_at, row.archived_by_id = None, None


def archived_events(db: Session, user_id: str) -> list[Event]:
    """Imported events archived whole: one that doesn't repeat, or the instances one change archived together.

    Repeating instances archived on their own are archived occurrences instead
    (event_occurrence_crud.get_archived_occurrences).
    """
    rows = (
        db.query(GoogleCalendarEvent)
        .filter(
            GoogleCalendarEvent.user_id == user_id,
            GoogleCalendarEvent.archived_at.isnot(None),
            GoogleCalendarEvent.deleted_at.is_(None),
            GoogleCalendarEvent.cancelled.is_(False),
        )
        .order_by(GoogleCalendarEvent.google_starts_at, GoogleCalendarEvent.id)
        .all()
    )
    seen: set[int] = set()
    out = []
    for row in rows:
        if row.archived_by_id is not None:
            if row.archived_by_id in seen:
                continue
            seen.add(row.archived_by_id)
        elif row.recurring_event_id is not None:
            continue
        out.append(event_out(row))
    return out


# ── Trash ────────────────────────────────────────────────────────────────────

def delete_event(db: Session, event_id: int, user_id: str, scope: EditScope) -> Event | None:
    """Move the instances in scope to the trash. Returns the instance, or None."""
    row = get_row(db, event_id, user_id)
    if row is None:
        return None
    scope = _resolve(row, scope)
    now = datetime.now(UTC)
    if scope == "this":
        row.deleted_at = now
    else:
        override = _override(db, row, scope, "delete", now, deleted_at=now)
        for target in _targets(db, row, scope):
            if target.deleted_at is None:
                target.deleted_at, target.deleted_by_id = now, override.id
    db.commit()
    db.refresh(row)
    return event_out(row)


def _deleted_with(db: Session, row: GoogleCalendarEvent) -> tuple[GoogleEventOverride | None, list]:
    """The change that deleted the row with others, and every row it deleted; else None and the row."""
    if row.deleted_by_id is None:
        return None, [row]
    rows = db.query(GoogleCalendarEvent).filter(
        GoogleCalendarEvent.deleted_by_id == row.deleted_by_id,
        GoogleCalendarEvent.deleted_at.isnot(None),
        GoogleCalendarEvent.cancelled.is_(False),
    ).all()
    return db.get(GoogleEventOverride, row.deleted_by_id), rows


def restore_event(db: Session, event_id: int, user_id: str) -> Event | None:
    """Bring an imported event back from the trash, with any deleted along with it. None if not there."""
    row = get_row(db, event_id, user_id, trashed=True)
    if row is None:
        return None
    override, rows = _deleted_with(db, row)
    if override is not None:
        # Restored, it no longer reaches instances later syncs import.
        override.deleted_at = None
    for each in rows:
        each.deleted_at, each.deleted_by_id = None, None
    db.commit()
    db.refresh(row)
    return event_out(row)


def purge_event(db: Session, event_id: int, user_id: str) -> bool:
    """Delete an imported event in the trash for good, with any deleted along with it. False if not there."""
    row = get_row(db, event_id, user_id, trashed=True)
    if row is None:
        return False
    override, rows = _deleted_with(db, row)
    if override is not None:
        override.deleted_at, override.cancelled = None, True
    for each in rows:
        each.deleted_at, each.cancelled = None, True
    db.commit()
    return True


def trash_items(db: Session, user_id: str) -> list[TrashItem]:
    """The user's imported events in the trash: one item for those deleted together.

    One instance of a repeating event deleted on its own carries its
    occurrence_date, like a kanso occurrence.
    """
    rows = (
        db.query(GoogleCalendarEvent)
        .filter(
            GoogleCalendarEvent.user_id == user_id,
            GoogleCalendarEvent.deleted_at > trash_cutoff(),
            GoogleCalendarEvent.cancelled.is_(False),
        )
        .order_by(GoogleCalendarEvent.google_starts_at, GoogleCalendarEvent.id)
        .all()
    )
    seen: set[int] = set()
    items = []
    for row in rows:
        if row.deleted_by_id is not None:
            if row.deleted_by_id in seen:
                continue
            seen.add(row.deleted_by_id)
        items.append(
            TrashItem(
                kind="event",
                id=-row.id,
                title=row.title,
                deleted_at=row.deleted_at,
                purge_at=row.deleted_at + TRASH_RETENTION,
                occurrence_date=row.start_date if row.recurring_event_id and row.deleted_by_id is None else None,
                source="google",
            )
        )
    return items


# ── Sync ─────────────────────────────────────────────────────────────────────

def apply_overrides(db: Session, user_id: str, new_rows: list[tuple[GoogleCalendarEvent, datetime | None]]) -> None:
    """Give instances a sync just imported every "following" and "all" change that reaches them.

    Each pair is a new row and when Google last changed it. Oldest first, so
    the latest change wins; an edit only if made after Google's last change.
    """
    series_ids = {row.recurring_event_id for row, _ in new_rows if row.recurring_event_id is not None}
    if not series_ids:
        return
    by_series: dict[str, list[GoogleEventOverride]] = {}
    for override in (
        db.query(GoogleEventOverride)
        .options(selectinload(GoogleEventOverride.tags))
        .filter(GoogleEventOverride.user_id == user_id, GoogleEventOverride.recurring_event_id.in_(series_ids))
        .order_by(GoogleEventOverride.set_at, GoogleEventOverride.id)
    ):
        by_series.setdefault(override.recurring_event_id, []).append(override)
    for row, updated in new_rows:
        for override in by_series.get(row.recurring_event_id, ()):
            if override.starts_from is None or row.google_starts_at >= override.starts_from:
                _apply_override(row, override, updated)


def _apply_override(row: GoogleCalendarEvent, override: GoogleEventOverride, updated: datetime | None) -> None:
    if override.kind == "edit":
        if updated is None or override.set_at >= updated:
            try:
                _edit_row(row, _loaded_changes(override.changes), override.set_at)
            except HTTPException:
                # The edit doesn't fit this instance (a timed edit missing a time for an all-day one).
                pass
    elif override.kind == "tags":
        row.tags = [] if override.use_default else list(override.tags)
        row.tags_overridden = not override.use_default
    elif override.kind in ("archive", "unarchive"):
        _archive_by(row, override, override.kind == "archive")
    elif override.kind == "delete":
        if override.cancelled:
            row.cancelled = True
        elif override.deleted_at is not None and row.deleted_at is None:
            row.deleted_at, row.deleted_by_id = override.deleted_at, override.id
