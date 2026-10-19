from sqlalchemy import Column, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.note_tag_model import note_tags


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        # Lists default to active rows only, so index just those per user.
        Index("ix_notes_user_id_active", "user_id", postgresql_where=text("archived_at IS NULL")),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False, default="Untitled Note")
    content = Column(Text, nullable=False, default="")
    created_date = Column(DateTime, server_default=func.now(), nullable=False)
    updated_date = Column(DateTime, server_default=func.now(), nullable=False)
    user_id = Column(String(36), nullable=True, index=True)
    # Set when the note is archived; NULL means active.
    archived_at = Column(DateTime(timezone=True), nullable=True)

    tags = relationship(
        "Tag",
        secondary=note_tags,
        back_populates="notes",
    )
