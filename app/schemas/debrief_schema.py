from datetime import date, time
from typing import Literal

from pydantic import BaseModel

from app.schemas.tag_schema import Tag as TagSchema


class DebriefTaskItem(BaseModel):
    id: int
    title: str
    category: str | None = None
    priority: int | None = None
    due_date: date | None = None
    due_time: time | None = None
    estimated_time: float | None = None
    tags: list[TagSchema] = []

    model_config = {"from_attributes": True}


class HabitDebriefStatus(BaseModel):
    id: int
    title: str
    current_streak: int = 0
    max_streak: int = 0
    logged_today: bool = False
    estimated_time: float | None = None
    tags: list[TagSchema] = []

    model_config = {"from_attributes": True}


class DebriefNoteItem(BaseModel):
    id: int
    title: str
    minutes: float = 0
    tags: list[TagSchema] = []

    model_config = {"from_attributes": True}


class WorkloadCapacity(BaseModel):
    is_rest_day: bool = False
    available_minutes: float | None = None
    committed_minutes: float = 0
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

    model_config = {"from_attributes": True}
