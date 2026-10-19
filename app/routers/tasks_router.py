from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.task_crud import (
    archive_task,
    create_task,
    delete_task,
    get_tasks,
    unarchive_task,
    update_task,
    update_task_status,
)
from app.database.database import get_db
from app.schemas.task_schema import Task, TaskCreate

router = APIRouter()


@router.get("/get-tasks", response_model=list[Task])
def read_tasks(
    archived: bool = Query(False, description="Return only archived tasks instead of active ones."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's active tasks, or their archived ones with ?archived=true."""
    return get_tasks(db, current_user.id, archived=archived)


@router.post("/create-task", response_model=Task)
def create_new_task(
    task: TaskCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new task for the authenticated user."""
    return create_task(db, task, current_user.id)


@router.delete("/del-task/{task_id}", response_model=Task)
def delete_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a task owned by the authenticated user."""
    return require_found(delete_task(db, task_id, current_user.id), "Task not found")


@router.patch("/update-task-status/{task_id}", response_model=Task)
def update_complete_status_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle the completion status of a task."""
    return require_found(update_task_status(db, task_id, current_user.id), "Task not found")


@router.put("/update-task/{task_id}", response_model=Task)
def update_task_by_id(
    task_id: int,
    payload: TaskCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace all fields of an existing task."""
    return require_found(update_task(db, task_id, payload, current_user.id), "Task not found")


@router.patch("/archive-task/{task_id}", response_model=Task)
def archive_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive a task along with its subtasks."""
    return require_found(archive_task(db, task_id, current_user.id), "Task not found")


@router.patch("/unarchive-task/{task_id}", response_model=Task)
def unarchive_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore an archived task and the subtasks archived with it."""
    return require_found(unarchive_task(db, task_id, current_user.id), "Task not found")
