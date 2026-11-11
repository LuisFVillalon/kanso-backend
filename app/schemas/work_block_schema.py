import re
from datetime import date, datetime, time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, PlainSerializer, model_validator

from app.schemas.limits import TIME_ZONE_MAX
from app.schemas.tag_schema import VisibleTags

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:00)?$")


def _parse_clock_time(value):
    """Read a local "HH:MM" time (":00" seconds allowed). Raises ValueError otherwise."""
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError("Expected a local time without a time zone")
        return value.replace(second=0, microsecond=0)
    if not isinstance(value, str) or not _HHMM.match(value):
        raise ValueError('Expected a local time as "HH:MM"')
    return time.fromisoformat(value[:5])


# A local wall-clock time, read and written as "HH:MM".
ClockTime = Annotated[
    time,
    BeforeValidator(_parse_clock_time),
    PlainSerializer(lambda t: t.strftime("%H:%M"), return_type=str),
]


def _check_time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {value!r}") from None
    return value


# An IANA time zone name, e.g. America/Los_Angeles.
TimeZoneName = Annotated[str, Field(max_length=TIME_ZONE_MAX), AfterValidator(_check_time_zone)]


def _check_span(start_time: time, end_time: time) -> None:
    if start_time == end_time:
        raise ValueError("A work block must end at a different time than it starts")


class WorkBlockCreate(BaseModel):
    # Exactly one: the task it's time to work on, or the habit it's time for.
    task_id: int | None = None
    habit_id: int | None = None
    # The local day and times, in time_zone. An end_time before start_time
    # runs past midnight into the next day.
    date: date
    start_time: ClockTime
    end_time: ClockTime
    # The browser's IANA zone.
    time_zone: TimeZoneName

    @model_validator(mode="after")
    def _check(self):
        if (self.task_id is None) == (self.habit_id is None):
            raise ValueError("A work block needs exactly one of task_id and habit_id")
        _check_span(self.start_time, self.end_time)
        return self


class WorkBlockUpdate(BaseModel):
    """A move or resize: the block's new local day and times."""

    date: date
    start_time: ClockTime
    end_time: ClockTime

    @model_validator(mode="after")
    def _check(self):
        _check_span(self.start_time, self.end_time)
        return self


class WorkBlock(BaseModel):
    id: int
    task_id: int | None = None
    habit_id: int | None = None
    # The task's or habit's title and tags.
    title: str
    tags: VisibleTags = []
    # Where it falls: the local day it starts on and its local times. On the
    # calendar and in the debrief they're in the caller's time zone, like
    # events; on create and update, in the block's own time_zone.
    date: date
    start_time: ClockTime
    end_time: ClockTime
    time_zone: str
    # The same span as UTC instants.
    starts_at: datetime
    ends_at: datetime
