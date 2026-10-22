from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_found
from app.crud.task_crud import create_task, get_tasks, set_linked_tasks, update_task, update_task_status
from app.crud.task_lifecycle_crud import (
    LinkedMode,
    archive_task,
    delete_task,
    purge_task,
    restore_task,
    unarchive_task,
)
from app.database.database import get_db
from app.schemas.task_schema import LinkedTasks, LinkedTasksUpdate, Task, TaskCreate

router = APIRouter()

_LINKED_QUERY = Query(
    "cascade",
    description="What happens to the task's linked tasks: 'cascade' (the default) applies the same action to them, "
    "'keep' unlinks them first so they stay as standalone tasks.",
)


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
    linked: LinkedMode = _LINKED_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move a task to the trash, where it can be restored for 30 days, with its subtasks unless ?linked=keep."""
    return require_found(delete_task(db, task_id, current_user.id, linked), "Task not found")


@router.patch("/update-task-status/{task_id}", response_model=Task)
def update_complete_status_by_id(
    task_id: int,
    with_linked: bool = Query(False, description="When completing the task, also complete its open linked tasks."),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle the completion status of a task, and with ?with_linked=true complete its open linked tasks too."""
    return require_found(update_task_status(db, task_id, current_user.id, with_linked), "Task not found")


@router.put("/update-task/{task_id}", response_model=Task)
def update_task_by_id(
    task_id: int,
    payload: TaskCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace all fields of an existing task."""
    return require_found(update_task(db, task_id, payload, current_user.id), "Task not found")


@router.put("/update-linked-tasks/{task_id}", response_model=LinkedTasks)
def update_linked_tasks_by_id(
    task_id: int,
    payload: LinkedTasksUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save a task's linked tasks in order, in one go: link, reorder, create new ones and unlink the rest."""
    parent, children = require_found(set_linked_tasks(db, task_id, payload.children, current_user.id), "Task not found")
    return {"parent": parent, "children": children}


@router.patch("/archive-task/{task_id}", response_model=Task)
def archive_task_by_id(
    task_id: int,
    linked: LinkedMode = _LINKED_QUERY,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive a task, along with its subtasks unless ?linked=keep."""
    return require_found(archive_task(db, task_id, current_user.id, linked), "Task not found")


@router.patch("/unarchive-task/{task_id}", response_model=Task)
def unarchive_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore an archived task, the subtasks archived with it, and any archived parents above it."""
    return require_found(unarchive_task(db, task_id, current_user.id), "Task not found")


@router.patch("/restore-task/{task_id}", response_model=Task)
def restore_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore a task from the trash, with the subtasks deleted along with it and any deleted parents above it."""
    return require_found(restore_task(db, task_id, current_user.id), "Task not found in the trash")


@router.delete("/purge-task/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def purge_task_by_id(
    task_id: int,
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a task that is in the trash, without waiting out the 30 days."""
    require_found(purge_task(db, task_id, current_user.id), "Task not found in the trash")
