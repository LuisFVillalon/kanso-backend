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
from app.models.mood_checkin_model import HiddenMoodTag, MoodCheckin
from app.schemas.mood_schema import (
    DEFAULT_MOOD_TAGS,
    MoodCheckinSave,
    MoodTags,
    default_mood_tag,
    normalize_mood_tag,
)
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


def _hidden_tags(db: Session, user_id: str) -> set[str]:
    return {tag for (tag,) in db.query(HiddenMoodTag.tag).filter(HiddenMoodTag.user_id == user_id)}


def _hide_default(db: Session, user_id: str, default: str) -> None:
    stmt = (
        insert(HiddenMoodTag)
        .values(user_id=user_id, tag=default)
        .on_conflict_do_nothing(constraint="uq_hidden_mood_tags_user_tag")
    )
    db.execute(stmt)


def _checkins_with_tag(db: Session, user_id: str, key: str) -> list[MoodCheckin]:
    """The user's check-ins carrying a tag that matches key (casefolded)."""
    rows = db.query(MoodCheckin).filter(MoodCheckin.user_id == user_id, func.cardinality(MoodCheckin.tags) > 0)
    return [row for row in rows if any(tag.casefold() == key for tag in row.tags)]


def rename_mood_tag(db: Session, user_id: str, old: str, new: str) -> int:
    """Rename a tag, ignoring case, on every one of the user's check-ins. Returns how many changed.

    A check-in that already has new keeps one copy, where the first of the two
    was. Renaming a default tag also hides it from the picker; renaming to a
    hidden default shows that default again. The caller checks old and new
    are non-blank and different.
    """
    new = normalize_mood_tag(new)
    old_key, new_key = old.strip().casefold(), new.casefold()
    changed = 0
    for row in _checkins_with_tag(db, user_id, old_key):
        renamed: dict[str, str] = {}
        for tag in row.tags:
            key = tag.casefold()
            if key in (old_key, new_key):
                key, tag = new_key, new
            renamed.setdefault(key, tag)
        if list(renamed.values()) != row.tags:
            row.tags = list(renamed.values())
            changed += 1
    old_default, new_default = default_mood_tag(old), default_mood_tag(new)
    if old_default and old_default != new_default:
        _hide_default(db, user_id, old_default)
    if new_default:
        db.query(HiddenMoodTag).filter(
            HiddenMoodTag.user_id == user_id, HiddenMoodTag.tag == new_default
        ).delete(synchronize_session=False)
    db.commit()
    return changed


def delete_mood_tag(db: Session, user_id: str, tag: str) -> int:
    """Remove a tag, ignoring case, from every one of the user's check-ins. Returns how many changed.

    Deleting a default tag also hides it from the picker.
    """
    key = tag.strip().casefold()
    rows = _checkins_with_tag(db, user_id, key)
    for row in rows:
        row.tags = [t for t in row.tags if t.casefold() != key]
    default = default_mood_tag(tag)
    if default:
        _hide_default(db, user_id, default)
    db.commit()
    return len(rows)


def get_mood_tags(db: Session, user_id: str) -> MoodTags:
    """The default mood tags the user hasn't hidden, plus the custom ones they use, most recently used first.

    A hidden default the user has typed in again since is listed with the custom tags.
    """
    used = func.unnest(MoodCheckin.tags).label("tag")
    rows = (
        db.query(used, MoodCheckin.period_start)
        .filter(MoodCheckin.user_id == user_id)
        .order_by(MoodCheckin.period_start.desc(), MoodCheckin.id.desc())
        .all()
    )
    hidden = _hidden_tags(db, user_id)
    shown = [tag for tag in DEFAULT_MOOD_TAGS if tag not in hidden]
    defaults = {tag.casefold() for tag in shown}
    custom: dict[str, str] = {}
    for tag, _ in rows:
        key = tag.casefold()
        if key not in defaults and key not in custom:
            custom[key] = tag
    return MoodTags(defaults=shown, custom=list(custom.values()))
