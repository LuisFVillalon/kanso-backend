from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.limits import NOTE_CONTENT_MAX, TAGS_PER_ITEM_MAX, TITLE_MAX
from app.schemas.tag_schema import Tag, TagCreate, VisibleTags


class NoteBase(BaseModel):
    title: str = "Untitled Note"
    content: str = ""
    tags: list[Tag] = []


class NoteCreate(NoteBase):
    # Limits live on the input models only, so rows saved before they existed
    # can still be read back through the Note response model.
    title: str = Field("Untitled Note", max_length=TITLE_MAX)
    content: str = Field("", max_length=NOTE_CONTENT_MAX)
    tags: list[TagCreate] = Field([], max_length=TAGS_PER_ITEM_MAX)


class NoteUpdate(BaseModel):
    title: str | None = Field(None, max_length=TITLE_MAX)
    content: str | None = Field(None, max_length=NOTE_CONTENT_MAX)
    tags: list[TagCreate] | None = Field(None, max_length=TAGS_PER_ITEM_MAX)


class Note(NoteBase):
    id: int
    created_date: datetime
    updated_date: datetime
    # user_id set server-side from JWT; exposed in responses, never in requests.
    user_id: str | None = None
    # Set server-side by /archive-note and /unarchive-note; NULL while active.
    archived_at: datetime | None = None
    # Set server-side by /del-note and /restore-note; NULL unless in the trash.
    deleted_at: datetime | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}
