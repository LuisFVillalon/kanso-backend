from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from app.schemas.limits import (
    CANVAS_ID_MAX,
    CANVAS_URL_MAX,
    LINKED_TASKS_MAX,
    REMINDER_MINUTES_MAX,
    SHORT_MAX,
    TAGS_PER_ITEM_MAX,
    TEXT_MAX,
    TITLE_MAX,
)
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
    # Set on tasks added from the Canvas panel: the Canvas assignment the task
    # follows (its assignment id, not a classic quiz's quiz_id) and its page.
    canvas_assignment_id: int | None = None
    canvas_html_url: str | None = None
    # An all-day calendar feed item's date (its `day`) when the task is linked,
    # which the feed sync compares like canvas_due_at.
    canvas_due_date: date | None = None

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
    canvas_assignment_id: int | None = Field(None, ge=1, le=CANVAS_ID_MAX)
    canvas_html_url: str | None = Field(None, max_length=CANVAS_URL_MAX)
    # Canvas's due_at for the assignment when the task is linked, so the first
    # sync can tell a due date the user changed from one Canvas changed.
    canvas_due_at: AwareDatetime | None = None

    @field_validator("canvas_html_url")
    @classmethod
    def https_url(cls, v):
        # The frontend renders it as a link, so only an absolute https URL
        # (never javascript:, a custom port or credentials) is stored.
        if v is None or v == "":
            return None
        try:
            parts = urlsplit(v)
            port = parts.port
        except ValueError:
            raise ValueError("canvas_html_url must be an https URL") from None
        if parts.scheme != "https" or not parts.hostname or port not in (None, 443) or parts.username or parts.password:
            raise ValueError("canvas_html_url must be an https URL")
        return v


class TaskParent(BaseModel):
    """The task directly above a linked task, enough to show it without fetching it."""

    id: int
    title: str

    model_config = {"from_attributes": True}


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
    # Set server-side: order among the parent's linked tasks, listed by
    # (position, id). NULL for top-level tasks.
    position: int | None = None
    # Set server-side: the task parent_task_id points at, or null for a
    # top-level task.
    parent_task: TaskParent | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}


class LinkedTaskRef(TaskCreate):
    """One of a parent's linked tasks: an existing task by id, or a new one.

    A new task takes the same fields, limits and checks as POST /create-task,
    so it can be created fully specified; only its title is required. Leaving
    out tags gives it the parent's tags.
    """

    id: int | None = None
    title: str | None = Field(None, max_length=TITLE_MAX)

    @field_validator("title")
    @classmethod
    def strip_title(cls, v):
        return v.strip() if v is not None else v

    @model_validator(mode="after")
    def id_or_title(self):
        if (self.id is None) == (not self.title):
            raise ValueError("Give either the id of an existing task or the title of a new one")
        if self.id is not None and self.model_fields_set != {"id"}:
            raise ValueError("An existing task is listed by its id alone")
        return self


class LinkedTasksUpdate(BaseModel):
    # Every active linked task of the parent, in order. Active ones left out are unlinked.
    children: list[LinkedTaskRef] = Field(max_length=LINKED_TASKS_MAX)


class LinkedTasks(BaseModel):
    parent: Task
    children: list[Task]
