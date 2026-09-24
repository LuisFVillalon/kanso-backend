from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.crud.base import get_owned
from app.models.tag_model import Tag as TagModel
from app.schemas.tag_schema import TagCreate


def get_or_create_tag(db: Session, tag_data, user_id: str) -> TagModel:
    """Return an existing tag matching name+user, or add a new one to the session.

    The caller is responsible for committing the session.  The tag is only
    *added* (not committed) so it can participate in the same transaction as
    the parent task or note.
    """
    tag = (
        db.query(TagModel)
        .filter(TagModel.name == tag_data.name, TagModel.user_id == user_id)
        .first()
    )
    if not tag:
        tag = TagModel(name=tag_data.name, color=tag_data.color, user_id=user_id)
        db.add(tag)
    return tag


def _ensure_name_available(db: Session, name: str, user_id: str, exclude_id: int | None = None) -> None:
    """Raise 409 if the user already has another tag with this name (uq_tags_name_user_id)."""
    q = db.query(TagModel).filter(TagModel.name == name, TagModel.user_id == user_id)
    if exclude_id is not None:
        q = q.filter(TagModel.id != exclude_id)
    if q.first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f'A tag named "{name}" already exists.')


def get_tags(db: Session, user_id: str):
    """Return all tags owned by the given user."""
    return db.query(TagModel).filter(TagModel.user_id == user_id).all()


def create_tag(db: Session, tag: TagCreate, user_id: str):
    """Create and persist a new tag, returning the saved record."""
    _ensure_name_available(db, tag.name, user_id)
    db_tag = TagModel(name=tag.name, color=tag.color, user_id=user_id)
    db.add(db_tag)
    db.commit()
    db.refresh(db_tag)
    return db_tag


def delete_tag(db: Session, tag_id: int, user_id: str):
    """Delete a tag by ID. Returns the deleted record, or None if not found."""
    tag = get_owned(db, TagModel, tag_id, user_id)
    if not tag:
        return None
    db.delete(tag)
    db.commit()
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
