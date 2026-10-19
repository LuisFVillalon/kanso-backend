from datetime import datetime
from typing import Literal

from pydantic import BaseModel

TrashKind = Literal["task", "note", "tag", "habit"]


class TrashItem(BaseModel):
    kind: TrashKind
    id: int
    # A tag's name, otherwise the item's title.
    title: str
    deleted_at: datetime
    # When the item is permanently deleted unless restored first.
    purge_at: datetime
    # Tasks only: the parent it was a subtask of, so subtasks deleted along
    # with their parent can be shown under it.
    parent_task_id: int | None = None
