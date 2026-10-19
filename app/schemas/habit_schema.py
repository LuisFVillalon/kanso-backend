from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.schemas.limits import TAGS_PER_ITEM_MAX, TITLE_MAX
from app.schemas.tag_schema import Tag, TagCreate, VisibleTags


class HabitBase(BaseModel):
    title: str
    tags: list[Tag] = []
    estimated_time: float | None = None

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


class HabitCreate(HabitBase):
    # Limits live on the input model only, so rows saved before they existed
    # can still be read back through the HabitResponse model.
    title: str = Field(max_length=TITLE_MAX)
    tags: list[TagCreate] = Field([], max_length=TAGS_PER_ITEM_MAX)


class HabitResponse(HabitBase):
    id: int
    user_id: str | None = None
    current_streak: int = 0
    max_streak: int = 0
    logged_today: bool = False
    # Set server-side by /archive-habit and /unarchive-habit; NULL while active.
    archived_at: datetime | None = None
    # Set server-side by /del-habit and /restore-habit; NULL unless in the trash.
    deleted_at: datetime | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}


class HabitHistoryEntry(BaseModel):
    date: str
    logged: bool


class HabitToggleDateRequest(BaseModel):
    date: str = Field(max_length=10)
    # Caller's local calendar date ("YYYY-MM-DD"). Used for the streak
    # grace-period ("logged today or yesterday?") calc so a server/client
    # timezone gap can't roll "today" over early. Optional; the server falls
    # back to its own date.today() when absent.
    local_date: str | None = Field(None, max_length=10)
