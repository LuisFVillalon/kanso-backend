from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

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


class TermDates(BaseModel):
    title: str
    start_date: date
    end_date: date


class NextTermSave(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class TermRolloverUpdate(BaseModel):
    auto_archive: bool


class TermRollover(BaseModel):
    """The user's rollover settings: auto_archive, the queued next term, and
    the last ended term left unarchived (auto_archive off), or null."""

    auto_archive: bool = False
    next_term: TermDates | None = None
    ended_term: TermDates | None = None
