from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.tag_crud import archive_tag, create_tag, delete_tag, get_tags, unarchive_tag, update_tag
from app.database.database import get_db
from app.schemas.tag_schema import Tag, TagCreate

router = APIRouter()


@router.get("/get-tags", response_model=list[Tag])
def read_tags(
    archived: bool = Query(False, description="Return only archived tags instead of active ones."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's active tags, or their archived ones with ?archived=true."""
    return get_tags(db, current_user.id, archived=archived)


@router.post("/create-tags", response_model=Tag)
def create_new_tag(
    tag: TagCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new tag for the authenticated user."""
    return create_tag(db, tag, current_user.id)


@router.delete("/del-tag/{tag_id}", response_model=Tag)
def delete_tag_by_id(
    tag_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a tag owned by the authenticated user."""
    return require_found(delete_tag(db, tag_id, current_user.id), "Tag not found")


@router.put("/update-tag/{tag_id}", response_model=Tag)
def update_tag_by_id(
    tag_id: int,
    payload: TagCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a tag's name and color."""
    return require_found(update_tag(db, tag_id, payload, current_user.id), "Tag not found")


@router.patch("/archive-tag/{tag_id}", response_model=Tag)
def archive_tag_by_id(
    tag_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive a tag. Tasks, notes and habits carrying it keep it attached."""
    return require_found(archive_tag(db, tag_id, current_user.id), "Tag not found")


@router.patch("/unarchive-tag/{tag_id}", response_model=Tag)
def unarchive_tag_by_id(
    tag_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore an archived tag."""
    return require_found(unarchive_tag(db, tag_id, current_user.id), "Tag not found")
