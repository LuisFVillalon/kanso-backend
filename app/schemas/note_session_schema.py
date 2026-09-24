from datetime import datetime

from pydantic import BaseModel


class NoteSessionStart(BaseModel):
    note_id: int


class NoteSession(BaseModel):
    id: int
    note_id: int
    started_at: datetime
    ended_at: datetime | None = None

    model_config = {"from_attributes": True}


class NoteTimeSpent(BaseModel):
    note_id: int
    total_seconds: int


class NoteSessionReapResult(BaseModel):
    reaped_count: int
