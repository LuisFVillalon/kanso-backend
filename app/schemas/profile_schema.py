from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.limits import SHORT_MAX
from app.schemas.mood_schema import MoodCheckinSettingsUpdate, ResolvedMoodSettings

_Short = Field(None, max_length=SHORT_MAX)

# The home panels switched on, in Tasks -> Notes -> Canvas order. "canvas,tasks"
# is the legacy left-then-right pair, kept so older saved profiles still validate.
HomePanels = Literal[
    "tasks",
    "notes",
    "canvas",
    "tasks,notes",
    "tasks,canvas",
    "notes,canvas",
    "tasks,notes,canvas",
    "canvas,tasks",
]

# A weekday, "0" (Sunday) to "6" (Saturday), the same numbering as rest_days.
Weekday = Literal["0", "1", "2", "3", "4", "5", "6"]
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class DayWindow(BaseModel):
    """One weekday's work window, as local "HH:MM" times on the same day."""

    start: str = Field(pattern=_HHMM)
    end: str = Field(pattern=_HHMM)

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class ProfileSave(BaseModel):
    name: str = Field(max_length=SHORT_MAX)
    shutoff_time: str | None = Field(None, max_length=5)
    avatar: str | None = _Short
    theme_accent: str | None = _Short
    page_style: str | None = _Short
    day_start_time: str | None = Field(None, max_length=5)
    rest_days: list[int] | None = Field(None, max_length=7)
    # Replaces the saved windows; a weekday left out uses day_start_time and shutoff_time.
    day_windows: dict[Weekday, DayWindow] | None = None
    layout_order: list[str] | None = Field(None, max_length=SHORT_MAX)
    layout_sizes: dict[str, str] | None = Field(None, max_length=SHORT_MAX)
    app_mode: str | None = _Short
    daily_brief_collapsed: bool | None = None
    dashboard_view: str | None = _Short
    notes_view_mode: str | None = _Short
    home_panels: HomePanels | None = None
    # Merges into the saved settings; null restores every default.
    mood_checkins: MoodCheckinSettingsUpdate | None = None


class ProfileOut(BaseModel):
    user_id: str
    name: str
    created_at: datetime
    shutoff_time: str | None = None
    avatar: str | None = None
    theme_accent: str | None = None
    page_style: str | None = None
    day_start_time: str | None = None
    rest_days: list[int] | None = None
    day_windows: dict[Weekday, DayWindow] | None = None
    layout_order: list[str] | None = None
    layout_sizes: dict[str, str] | None = None
    app_mode: str | None = None
    daily_brief_collapsed: bool | None = None
    dashboard_view: str | None = None
    notes_view_mode: str | None = None
    home_panels: str | None = None
    mood_checkins: ResolvedMoodSettings = None

    model_config = {"from_attributes": True}
