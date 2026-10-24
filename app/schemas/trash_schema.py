from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.task_schema import TaskParent

TrashKind = Literal["task", "note", "tag", "habit", "event"]


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
    # Tasks only: that parent's id and title, whether it is live or in the trash too.
    parent_task: TaskParent | None = None
    # Events only: set when one occurrence of a repeating event was deleted
    # rather than the series, which id names. Pass it back to restore or
    # purge just that occurrence.
    occurrence_date: date | None = None
