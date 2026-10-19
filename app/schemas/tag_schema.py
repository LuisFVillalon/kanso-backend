from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from app.schemas.limits import COLOR_MAX, TAG_NAME_MAX


class TagBase(BaseModel):
    name: str
    color: str | None = None


class TagCreate(TagBase):
    # Limits live on the input models only, so rows saved before they existed
    # can still be read back through the response models.
    name: str = Field(min_length=1, max_length=TAG_NAME_MAX)
    color: str | None = Field(None, max_length=COLOR_MAX)


class Tag(TagBase):
    id: int
    # user_id exposed in responses so the frontend knows which tags belong to it.
    user_id: str | None = None
    # NULL while active. Archived tags still appear in task/note/habit tag
    # lists, so the client uses this to tell them apart.
    archived_at: datetime | None = None
    # NULL unless the tag is in the trash.
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


def _drop_deleted_tags(tags):
    return [t for t in tags if getattr(t, "deleted_at", None) is None]


# An item's tags as shown to the client. A deleted tag stays linked to its
# items so restoring it brings the links back, but until then it is hidden.
VisibleTags = Annotated[list[Tag], BeforeValidator(_drop_deleted_tags)]
