from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr

from app.schemas.limits import CANVAS_HIDDEN_COURSES_MAX, CANVAS_URL_MAX

CanvasStatus = Literal["active", "invalid", "error"]


class CanvasConnect(BaseModel):
    # The school's Canvas address: a bare host or any page URL on it.
    base_url: str = Field(max_length=CANVAS_URL_MAX)
    # A SecretStr so it can't leak through a repr, a log line or a
    # validation error echoing its input; its format is checked in crud.
    token: SecretStr
    # Replacing the token with one for a different Canvas account or school
    # is refused with canvas_account_changed unless this is true.
    allow_account_change: bool = False


class CanvasConnectionUpdate(BaseModel):
    hidden_course_ids: list[Annotated[int, Field(ge=1)]] = Field(max_length=CANVAS_HIDDEN_COURSES_MAX)


class CanvasConnectionOut(BaseModel):
    """The connection's masked status. Never carries the token itself."""

    connected: bool
    # "invalid" means Canvas rejected the token or it has expired: reconnect.
    status: CanvasStatus | None = None
    base_url: str | None = None
    token_last4: str | None = None
    canvas_user_id: int | None = None
    canvas_user_name: str | None = None
    token_expires_at: datetime | None = None
    hidden_course_ids: list[int] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_error_at: datetime | None = None
