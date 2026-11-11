from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.work_block_crud import create_work_block, delete_work_block, update_work_block
from app.database.database import get_db
from app.schemas.work_block_schema import WorkBlock, WorkBlockCreate, WorkBlockUpdate

router = APIRouter()


@router.post("/create-work-block", response_model=WorkBlock)
def create_block(
    payload: WorkBlockCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Plan a "work on it" time on the calendar for one of the user's open tasks, or one of their habits."""
    return create_work_block(db, payload, current_user.id)


@router.put("/update-work-block/{block_id}", response_model=WorkBlock)
def update_block(
    block_id: int,
    payload: WorkBlockUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move or resize a work block."""
    return require_found(update_work_block(db, block_id, payload, current_user.id), "Work block not found")


@router.delete("/delete-work-block/{block_id}", response_model=WorkBlock)
def delete_block(
    block_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a work block. Returns what it held."""
    return require_found(delete_work_block(db, block_id, current_user.id), "Work block not found")
