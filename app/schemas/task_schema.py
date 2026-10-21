from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.schemas.limits import REMINDER_MINUTES_MAX, SHORT_MAX, TAGS_PER_ITEM_MAX, TEXT_MAX, TITLE_MAX
from app.schemas.tag_schema import TagCreate, VisibleTags


class TaskBase(BaseModel):
    title: str
    description: str | None = None
    category: str | None = None
    completed: bool = False
    priority: int | None = None

    due_date: date | None = None
    due_time: time | None = None

    completed_date: datetime | None = None

    estimated_time: float | None = None
    parent_task_id: int | None = None
    # Minutes before the due time to send the reminder; null uses the user's
    # default from the notification preferences.
    reminder_minutes: int | None = None

    tags: list[TagCreate] = []

    @field_validator("priority", mode="before")
    @classmethod
    def validate_priority(cls, v):
        if v is None or v == 0:
            return None
        if not isinstance(v, int) or v < 1:
            raise ValueError("Priority must be a positive integer (≥ 1)")
        return v

    @field_validator("due_date", "due_time", mode="before")
    @classmethod
    def empty_string_to_none(cls, v):
        if v == "":
            return None
        return v

    @field_validator("estimated_time", mode="before")
    @classmethod
    def validate_estimated_time(cls, v):
        if v is None:
            return v
        if isinstance(v, Decimal):
            v = float(v)
        if v < 0:
            raise ValueError("Estimated time must be non-negative")
        return v


class TaskCreate(TaskBase):
    # Limits live on the input model only, so rows saved before they existed
    # can still be read back through the Task response model.
    title: str = Field(max_length=TITLE_MAX)
    description: str | None = Field(None, max_length=TEXT_MAX)
    category: str | None = Field(None, max_length=SHORT_MAX)
    reminder_minutes: int | None = Field(None, ge=1, le=REMINDER_MINUTES_MAX)
    tags: list[TagCreate] = Field([], max_length=TAGS_PER_ITEM_MAX)


class Task(TaskBase):
    id: int
    created_date: datetime
    # user_id is set server-side from the JWT — exposed in responses but never
    # accepted from the client in TaskCreate/TaskBase.
    user_id: str | None = None
    # Set server-side by /archive-task and /unarchive-task; NULL while active.
    archived_at: datetime | None = None
    # Set server-side by /del-task and /restore-task; NULL unless in the trash.
    deleted_at: datetime | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}
