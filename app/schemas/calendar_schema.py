from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.limits import TITLE_MAX


class CalendarSettingsUpdate(BaseModel):
    title: str | None = Field(None, max_length=TITLE_MAX)
    start_date: date | None = None
    end_date: date | None = None


class CalendarSettings(BaseModel):
    id: int
    title: str
    start_date: date
    end_date: date

    model_config = {"from_attributes": True}


class ArchivedTerm(BaseModel):
    id: int
    title: str
    start_date: date
    end_date: date
    archived_at: datetime

    model_config = {"from_attributes": True}
