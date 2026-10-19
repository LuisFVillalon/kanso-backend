from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.crud.trash_crud import get_trash
from app.database.database import get_db
from app.schemas.trash_schema import TrashItem, TrashKind

router = APIRouter()


@router.get("/get-trash", response_model=list[TrashItem])
def read_trash(
    kind: TrashKind | None = Query(None, description="Only list deleted items of this kind."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's deleted tasks, notes, tags and habits, newest first.

    Each stays restorable via /restore-{kind}/{id} until its purge_at, 30 days
    after deletion; items past that are permanently deleted.
    """
    return get_trash(db, current_user.id, kind=kind)
