"""
Mood check-ins: one row per user, period kind (day, week, month) and period start.

Saving is an upsert on that key, so creating, editing and backfilling a
check-in are the same call. Deleting removes the row outright; there is no
trash, and the client's Undo saves it again. Mood data is private to the
user and is never read by the daily debrief or any AI feature.
"""

from datetime import date

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.models.mood_checkin_model import MoodCheckin
from app.schemas.mood_schema import DEFAULT_MOOD_TAGS, MoodCheckinSave, MoodTags
from app.schemas.mood_schema import MoodCheckin as MoodCheckinOut


def get_checkins(db: Session, user_id: str, start: date, end: date, kinds: list[str]) -> list[MoodCheckin]:
    """The user's check-ins of the given kinds whose period_start falls in start..end inclusive."""
    return (
        db.query(MoodCheckin)
        .filter(
            MoodCheckin.user_id == user_id,
            MoodCheckin.period_kind.in_(kinds),
            MoodCheckin.period_start >= start,
            MoodCheckin.period_start <= end,
        )
        .order_by(MoodCheckin.period_start, MoodCheckin.period_kind)
        .all()
    )


def save_checkin(db: Session, user_id: str, body: MoodCheckinSave) -> MoodCheckin:
    """Insert or replace the user's check-in for body's period. Returns the saved row."""
    values = body.model_dump()
    replaced = {k: v for k, v in values.items() if k not in ("period_kind", "period_start")}
    stmt = (
        insert(MoodCheckin)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(
            constraint="uq_mood_checkins_user_kind_start",
            set_={**replaced, "updated_at": func.now()},
        )
        .returning(MoodCheckin)
    )
    row = db.scalars(stmt, execution_options={"populate_existing": True}).one()
    db.commit()
    db.refresh(row)
    return row


def delete_checkin(db: Session, checkin_id: int, user_id: str) -> MoodCheckinOut | None:
    """Delete one of the user's check-ins. Returns what it held, or None if not found."""
    row = get_owned(db, MoodCheckin, checkin_id, user_id)
    if row is None:
        return None
    # Read it out first: a deleted row can't be loaded again after the commit.
    deleted = MoodCheckinOut.model_validate(row)
    db.delete(row)
    db.commit()
    return deleted


def get_mood_tags(db: Session, user_id: str) -> MoodTags:
    """The default mood tags, plus the custom ones the user has used, most recently used first."""
    used = func.unnest(MoodCheckin.tags).label("tag")
    rows = (
        db.query(used, MoodCheckin.period_start)
        .filter(MoodCheckin.user_id == user_id)
        .order_by(MoodCheckin.period_start.desc(), MoodCheckin.id.desc())
        .all()
    )
    defaults = {tag.casefold() for tag in DEFAULT_MOOD_TAGS}
    custom: dict[str, str] = {}
    for tag, _ in rows:
        key = tag.casefold()
        if key not in defaults and key not in custom:
            custom[key] = tag
    return MoodTags(defaults=list(DEFAULT_MOOD_TAGS), custom=list(custom.values()))
