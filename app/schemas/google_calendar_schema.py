from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.limits import TAGS_PER_ITEM_MAX
from app.schemas.tag_schema import Tag


class GoogleCalendarStatus(BaseModel):
    # False until the server has its Google OAuth values: hide Connect.
    configured: bool
    connected: bool
    email: str | None = None
    last_synced_at: datetime | None = None
    # A stable code from the last sync (core/google_calendar.py); null if it worked.
    last_error: str | None = None


class GoogleCalendarConnectOut(BaseModel):
    authorization_url: str


class GoogleCalendarOut(BaseModel):
    id: int
    name: str
    color: str | None = None
    selected: bool
    default_tag: Tag | None = None


class GoogleCalendarUpdate(BaseModel):
    """The settings to change; any left out keep their value."""

    selected: bool | None = None
    # null clears the calendar's default tag.
    default_tag_id: int | None = None


class GoogleEventTagsUpdate(BaseModel):
    tag_ids: list[int] = Field(max_length=TAGS_PER_ITEM_MAX)
    # With an empty tag_ids, returns the event to its calendar's default tag.
    # An empty tag_ids without it leaves the event deliberately untagged.
    use_default: bool = False

    @model_validator(mode="after")
    def default_means_no_tags(self):
        if self.use_default and self.tag_ids:
            raise ValueError("use_default needs an empty tag_ids")
        return self


class GoogleEventTagsOut(BaseModel):
    # The occurrence's event_id.
    event_id: int
    # The tags the event now shows: its own, else its calendar's default.
    tags: list[Tag] = []
    use_default: bool
