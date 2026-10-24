from datetime import date, time
from typing import Literal

from pydantic import BaseModel

from app.schemas.event_schema import EventOccurrence, EventTagMinutes
from app.schemas.tag_schema import VisibleTags


class DebriefTaskItem(BaseModel):
    id: int
    title: str
    category: str | None = None
    priority: int | None = None
    due_date: date | None = None
    due_time: time | None = None
    estimated_time: float | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}


class HabitDebriefStatus(BaseModel):
    id: int
    title: str
    current_streak: int = 0
    max_streak: int = 0
    logged_today: bool = False
    estimated_time: float | None = None
    tags: VisibleTags = []

    model_config = {"from_attributes": True}


class DebriefNoteItem(BaseModel):
    id: int
    title: str
    minutes: float = 0
    tags: VisibleTags = []

    model_config = {"from_attributes": True}


class WorkloadCapacity(BaseModel):
    is_rest_day: bool = False
    # Today's work window, from the profile; null if it has no day start and
    # "Call it a day" set.
    window_minutes: float | None = None
    # Minutes of the window today's events take, overlaps counted once.
    # Archived and all-day events take none.
    event_minutes: float = 0
    # What's left of the window for tasks: window_minutes - event_minutes.
    available_minutes: float | None = None
    # Estimated minutes of open tasks due today.
    committed_minutes: float = 0
    # available_minutes - committed_minutes, at least 0.
    free_minutes: float | None = None
    utilization_pct: float | None = None
    is_overcommitted: bool = False


class FocusNextItem(BaseModel):
    task_id: int
    title: str
    priority: int | None = None
    due_date: date | None = None
    estimated_time: float | None = None
    reason: Literal["high_priority", "upcoming_high_effort"]

    model_config = {"from_attributes": True}


class DailyDebriefReport(BaseModel):
    report_date: date
    overdue_tasks: list[DebriefTaskItem] = []
    due_today_tasks: list[DebriefTaskItem] = []
    completed_today_tasks: list[DebriefTaskItem] = []
    notes_worked_today: list[DebriefNoteItem] = []
    habit_status: list[HabitDebriefStatus] = []
    workload: WorkloadCapacity
    focus_next: list[FocusNextItem] = []
    # Today's event occurrences in start order, as the calendar shows them
    # (past archived ones included and flagged).
    events_today: list[EventOccurrence] = []
    # The ones that have ended by local_time, archived ones left out: today's
    # activity. Empty without local_time.
    passed_events_today: list[EventOccurrence] = []
    # Today's event minutes per tag, for the time allocation bar, and the
    # same for the passed ones, for the activity log. Archived events are
    # left out of both.
    event_minutes_by_tag: list[EventTagMinutes] = []
    passed_event_minutes_by_tag: list[EventTagMinutes] = []

    model_config = {"from_attributes": True}
