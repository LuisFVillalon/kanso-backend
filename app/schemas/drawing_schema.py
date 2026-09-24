from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.limits import DRAWING_DATA_URL_MAX


class DrawingSave(BaseModel):
    image_data_url: str = Field(max_length=DRAWING_DATA_URL_MAX)


class DrawingOut(BaseModel):
    user_id: str
    image_data_url: str
    updated_at: datetime

    model_config = {"from_attributes": True}
