from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.limits import SHORT_MAX

_Short = Field(None, max_length=SHORT_MAX)


class ProfileSave(BaseModel):
    name: str = Field(max_length=SHORT_MAX)
    shutoff_time: str | None = Field(None, max_length=5)
    avatar: str | None = _Short
    theme_accent: str | None = _Short
    page_style: str | None = _Short
    day_start_time: str | None = Field(None, max_length=5)
    rest_days: list[int] | None = Field(None, max_length=7)
    layout_order: list[str] | None = Field(None, max_length=SHORT_MAX)
    layout_sizes: dict[str, str] | None = Field(None, max_length=SHORT_MAX)
    app_mode: str | None = _Short
    daily_brief_collapsed: bool | None = None
    dashboard_view: str | None = _Short
    notes_view_mode: str | None = _Short


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
    layout_order: list[str] | None = None
    layout_sizes: dict[str, str] | None = None
    app_mode: str | None = None
    daily_brief_collapsed: bool | None = None
    dashboard_view: str | None = None
    notes_view_mode: str | None = None

    model_config = {"from_attributes": True}
