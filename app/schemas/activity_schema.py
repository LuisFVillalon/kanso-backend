from datetime import UTC, date, datetime, time
from typing import Annotated

from pydantic import AfterValidator, BaseModel

from app.schemas.tag_schema import VisibleTags


def _as_utc(value: datetime) -> datetime:
    """Mark a stored naive UTC timestamp as UTC, so it serializes with an offset."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# Task completion and note session timestamps are stored as naive UTC. The
# activity response labels them, so the client can place them on its own
# local calendar without guessing.
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class ActivityTask(BaseModel):
    id: int
    title: str
    category: str | None = None
    priority: int | None = None
    due_date: date | None = None
    due_time: time | None = None
    estimated_time: float | None = None
    completed: bool = False
    completed_date: UtcDatetime | None = None
    parent_task_id: int | None = None
    tags: VisibleTags = []
    archived: bool = False

    model_config = {"from_attributes": True}


class ActivityNoteSession(BaseModel):
    id: int
    note_id: int
    note_title: str
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: int
    # Whether the session's note is archived.
    archived: bool = False


class ActivityHabitCompletion(BaseModel):
    habit_id: int
    habit_title: str
    date: date
    estimated_time: float | None = None
    # Whether the habit is archived.
    archived: bool = False


class CalendarActivity(BaseModel):
    start_date: date
    end_date: date
    # Tasks due on a day in the range, completed or not.
    scheduled_tasks: list[ActivityTask] = []
    # Tasks completed during the range, whatever their due date.
    completed_tasks: list[ActivityTask] = []
    # Closed note editing sessions that overlap the range.
    note_sessions: list[ActivityNoteSession] = []
    habit_completions: list[ActivityHabitCompletion] = []
