from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func

from app.database.database import Base


class NoteSession(Base):
    __tablename__ = "note_sessions"
    __table_args__ = (
        # The calendar activity range reads a user's sessions by when they ended.
        Index("ix_note_sessions_user_id_ended_at", "user_id", "ended_at"),
    )

    id = Column(Integer, primary_key=True)
    note_id = Column(Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), nullable=True, index=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    ended_at = Column(DateTime, nullable=True)
