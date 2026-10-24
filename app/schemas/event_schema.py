import datetime as dt
from datetime import date, datetime, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.limits import (
    REMINDER_MINUTES_MAX,
    REPEAT_INTERVAL_MAX,
    TAGS_PER_ITEM_MAX,
    TEXT_MAX,
    TIME_ZONE_MAX,
    TITLE_MAX,
)
from app.schemas.tag_schema import Tag, TagCreate, VisibleTags

# Which occurrences an edit, archive or delete of a repeating event applies to.
EditScope = Literal["this", "following", "all"]
# 0=Sun..6=Sat, as Profile.rest_days.
Weekday = Annotated[int, Field(ge=0, le=6)]


def check_series(values: dict) -> dict:
    """Normalize a series' fields and check they fit together. Raises ValueError if not.

    Used on create, and on the merged fields of an edit, since a partial edit
    can only be checked against what it leaves unchanged.
    """
    try:
        ZoneInfo(values["time_zone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {values['time_zone']!r}") from None
    if values["all_day"]:
        values["start_time"] = values["end_time"] = None
    else:
        if values["start_time"] is None or values["end_time"] is None:
            raise ValueError("A timed event needs both start_time and end_time")
        if values["start_time"] == values["end_time"]:
            raise ValueError("An event must end at a different time than it starts")
    values["repeat_days"] = sorted(set(values["repeat_days"]))
    if not values["repeat_days"]:
        values["end_date"] = None
        values["repeat_interval"] = 1
    elif values["end_date"] is not None and values["end_date"] < values["start_date"]:
        raise ValueError("end_date must not be before start_date")
    return values


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    description: str | None = Field(None, max_length=TEXT_MAX)
    tags: list[TagCreate] = Field([], max_length=TAGS_PER_ITEM_MAX)
    all_day: bool = False
    # The first day; for a repeating event, the first day it can repeat on.
    start_date: date
    # The last day a repeating event can repeat on; null repeats forever.
    # Ignored for a one-off event.
    end_date: date | None = None
    # Local times in time_zone; ignored for an all-day event. An end_time
    # before start_time runs past midnight into the next day.
    start_time: time | None = None
    end_time: time | None = None
    # The browser's IANA time zone, e.g. America/Los_Angeles.
    time_zone: str = Field(max_length=TIME_ZONE_MAX)
    # Weekdays to repeat on, 0=Sun..6=Sat; empty for a one-off event.
    repeat_days: list[Weekday] = Field([], max_length=7)
    # Every this many weeks (2 for biweekly).
    repeat_interval: int = Field(1, ge=1, le=REPEAT_INTERVAL_MAX)
    # Minutes before each occurrence to remind; null for no reminder.
    reminder_minutes: int | None = Field(None, ge=0, le=REMINDER_MINUTES_MAX)

    @model_validator(mode="after")
    def series_fits(self):
        for key, value in check_series(self.model_dump(exclude={"tags"})).items():
            setattr(self, key, value)
        return self


class EventUpdate(BaseModel):
    """The fields to change; any left out keep their value.

    With scope=this only title, description, tags, all_day, start_time,
    end_time and date apply, to that one occurrence; date moves it to another
    day. The series fields (start_date, end_date, time_zone, repeat_*,
    reminder_minutes) apply only to "following" or "all".
    """

    title: str | None = Field(None, min_length=1, max_length=TITLE_MAX)
    description: str | None = Field(None, max_length=TEXT_MAX)
    tags: list[TagCreate] | None = Field(None, max_length=TAGS_PER_ITEM_MAX)
    all_day: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    time_zone: str | None = Field(None, max_length=TIME_ZONE_MAX)
    repeat_days: list[Weekday] | None = Field(None, max_length=7)
    repeat_interval: int | None = Field(None, ge=1, le=REPEAT_INTERVAL_MAX)
    reminder_minutes: int | None = Field(None, ge=0, le=REMINDER_MINUTES_MAX)
    # Moves this one occurrence to another day (scope=this only). Spelled
    # dt.date because the field's name shadows the type.
    date: dt.date | None = None

    @field_validator("title", "all_day", "start_date", "time_zone", "repeat_days", "repeat_interval")
    @classmethod
    def not_null(cls, v):
        # Fields the series can't be without: leaving them out keeps them,
        # but null would clear them.
        if v is None:
            raise ValueError("may be left out but not null")
        return v


class Event(BaseModel):
    id: int
    title: str
    description: str | None = None
    tags: VisibleTags = []
    all_day: bool
    start_date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    time_zone: str
    repeat_days: list[int] = []
    repeat_interval: int = 1
    reminder_minutes: int | None = None
    split_from_id: int | None = None
    created_date: datetime
    # NULL while active; set when the whole series is archived.
    archived_at: datetime | None = None
    # NULL unless the series is in the trash.
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class EventOccurrence(BaseModel):
    """One occurrence of an event, as placed on the caller's calendar."""

    event_id: int
    # The day the series' rule puts it on (in the series' time zone). Pass it
    # back as occurrence_date to edit, archive or delete this occurrence.
    occurrence_date: date
    title: str
    description: str | None = None
    tags: VisibleTags = []
    all_day: bool
    # Where it falls in the caller's time zone: the local day and time it
    # starts and ends. end_date is after date when it runs past midnight. An
    # all-day event keeps its own day in every time zone, with null times.
    date: date
    end_date: date
    start_time: time | None = None
    end_time: time | None = None
    # The same span as UTC instants. An all-day event spans its day's
    # midnights in the series' time zone.
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    repeat_days: list[int] = []
    repeat_interval: int = 1
    reminder_minutes: int | None = None
    # True when this occurrence was edited or moved on its own.
    edited: bool = False
    # Ended before now.
    past: bool = False
    # Archived, on its own or with its series.
    archived: bool = False


class EventTagMinutes(BaseModel):
    # null for events without a tag.
    tag: Tag | None = None
    minutes: float = 0
    count: int = 0


class EventReportDay(BaseModel):
    date: date
    count: int = 0
    # Minutes of events on this day (an event past midnight counts on both days).
    minutes: float = 0
    is_rest_day: bool = False
    # The work window's length, 0 on a rest day.
    window_minutes: float = 0
    # Minutes inside the work window taken by events; overlapping events count once.
    event_window_minutes: float = 0
    # window_minutes - event_window_minutes.
    free_minutes: float = 0


class EventReportPeriod(BaseModel):
    # The period's first day: a Sunday for weeks, the 1st for months.
    start: date
    count: int = 0
    minutes: float = 0


class EventReport(BaseModel):
    start_date: date
    end_date: date
    # Occurrences that start in the range: past ones (archived or not) and
    # upcoming ones that aren't archived; upcoming archived ones are left out.
    event_count: int = 0
    # Past archived occurrences in the range; upcoming archived ones are left out.
    archived_count: int = 0
    # Minutes of events in the range. All-day events count but add no minutes.
    total_minutes: float = 0
    # Free time in the work windows: window_minutes is every day's work
    # window added up (rest days add none), event_window_minutes the part of
    # it events took, free_minutes what's left and free_pct its share of
    # window_minutes.
    window_minutes: float = 0
    event_window_minutes: float = 0
    free_minutes: float = 0
    free_pct: float | None = None
    # The day with the most event minutes; null if there are none.
    busiest_day: EventReportDay | None = None
    # Minutes per tag; an event with several tags counts in full under each.
    by_tag: list[EventTagMinutes] = []
    by_day: list[EventReportDay] = []
    by_week: list[EventReportPeriod] = []
    by_month: list[EventReportPeriod] = []
