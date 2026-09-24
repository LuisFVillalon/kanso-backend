from pydantic import BaseModel, Field

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

    model_config = {"from_attributes": True}
