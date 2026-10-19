from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.crud.base import archived_filter, deleted_filter, get_owned, set_archived, trash_cutoff
from app.crud.trash_crud import purge_expired, purge_from_trash, restore_from_trash
from app.models.tag_model import Tag as TagModel
from app.schemas.tag_schema import TagCreate


def _tag_named(db: Session, name: str, user_id: str, exclude_id: int | None = None) -> TagModel | None:
    """The user's tag with this name, archived and deleted ones included (uq_tags_name_user_id).

    A deleted tag past its trash retention is gone, so it's purged here
    rather than returned, freeing the name.
    """
    q = db.query(TagModel).filter(TagModel.name == name, TagModel.user_id == user_id)
    if exclude_id is not None:
        q = q.filter(TagModel.id != exclude_id)
    tag = q.first()
    if tag and tag.deleted_at is not None and tag.deleted_at <= trash_cutoff():
        db.delete(tag)
        db.flush()
        return None
    return tag


def get_or_create_tag(db: Session, tag_data, user_id: str) -> TagModel:
    """Return an existing tag matching name+user, or add a new one to the session.

    The caller is responsible for committing the session.  The tag is only
    *added* (not committed) so it can participate in the same transaction as
    the parent task or note.
    """
    tag = _tag_named(db, tag_data.name, user_id)
    if not tag:
        tag = TagModel(name=tag_data.name, color=tag_data.color, user_id=user_id)
        db.add(tag)
    return tag


def set_item_tags(db: Session, item, tags_data, user_id: str) -> None:
    """Replace a task's, note's or habit's tags by name.

    An archived tag the item didn't already carry is restored, since newly
    attaching it means the user wants it. Tags already on the item stay
    archived when the item is re-saved.

    Deleted tags are hidden from the item's tag list, so the client never
    sends them back: their links are kept, ready for the tag's restore.
    Naming a deleted tag attaches it as new, which restores it.
    """
    previous_ids = {t.id for t in item.tags}
    for tag in [t for t in item.tags if t.deleted_at is None]:
        item.tags.remove(tag)
    for tag_data in tags_data:
        tag = get_or_create_tag(db, tag_data, user_id)
        if tag.deleted_at is not None or tag.id not in previous_ids:
            tag.archived_at = None
            tag.deleted_at = None
        if tag not in item.tags:
            item.tags.append(tag)


def _ensure_name_available(db: Session, name: str, user_id: str, exclude_id: int | None = None) -> None:
    """Raise 409 if the user already has another tag with this name (uq_tags_name_user_id)."""
    existing = _tag_named(db, name, user_id, exclude_id)
    if existing:
        if existing.deleted_at is not None:
            where = " in Recently Deleted"
        elif existing.archived_at is not None:
            where = " in your archive"
        else:
            where = ""
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f'A tag named "{name}" already exists{where}.'
        )


def get_tags(db: Session, user_id: str, archived: bool = False):
    """Return the user's active tags, or only their archived ones when archived=True. Deleted tags are left out."""
    return (
        db.query(TagModel)
        .filter(TagModel.user_id == user_id, deleted_filter(TagModel, False), archived_filter(TagModel, archived))
        .all()
    )


def create_tag(db: Session, tag: TagCreate, user_id: str):
    """Create and persist a new tag, returning the saved record."""
    _ensure_name_available(db, tag.name, user_id)
    db_tag = TagModel(name=tag.name, color=tag.color, user_id=user_id)
    db.add(db_tag)
    db.commit()
    db.refresh(db_tag)
    return db_tag


def delete_tag(db: Session, tag_id: int, user_id: str):
    """Move a tag to the trash. Its links are kept but hidden. Returns None if not found."""
    tag = get_owned(db, TagModel, tag_id, user_id)
    if not tag:
        return None
    tag.deleted_at = datetime.now(UTC)
    purge_expired(db, user_id)
    db.commit()
    db.refresh(tag)
    return tag


def update_tag(db: Session, tag_id: int, tag: TagCreate, user_id: str):
    """Update a tag's name and color. Returns None if not found."""
    db_tag = get_owned(db, TagModel, tag_id, user_id)
    if not db_tag:
        return None
    _ensure_name_available(db, tag.name, user_id, exclude_id=tag_id)
    db_tag.name = tag.name
    db_tag.color = tag.color
    db.commit()
    db.refresh(db_tag)
    return db_tag


def archive_tag(db: Session, tag_id: int, user_id: str):
    """Archive a tag. Its task/note/habit links are kept. Returns None if not found."""
    return set_archived(db, TagModel, tag_id, user_id, archived=True)


def unarchive_tag(db: Session, tag_id: int, user_id: str):
    """Restore an archived tag. Returns None if not found."""
    return set_archived(db, TagModel, tag_id, user_id, archived=False)


def restore_tag(db: Session, tag_id: int, user_id: str):
    """Restore a tag from the trash, back on every item it was on. Returns None if not in the trash."""
    return restore_from_trash(db, TagModel, tag_id, user_id)


def purge_tag(db: Session, tag_id: int, user_id: str):
    """Permanently delete a tag in the trash. Returns None if not in the trash."""
    return purge_from_trash(db, TagModel, tag_id, user_id)
