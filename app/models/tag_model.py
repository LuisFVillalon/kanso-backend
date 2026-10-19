from sqlalchemy import Column, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.habit_tag_model import habit_tags
from app.models.note_tag_model import note_tags
from app.models.task_tag_model import task_tags


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        # Each user can have their own tag with the same name.
        # NULLs are treated as distinct in Postgres, so legacy rows (user_id=NULL)
        # with the same name are also allowed through.
        UniqueConstraint("name", "user_id", name="uq_tags_name_user_id"),
        # Lists default to active, undeleted rows only, so index just those per user.
        Index(
            "ix_tags_user_id_active", "user_id", postgresql_where=text("archived_at IS NULL AND deleted_at IS NULL")
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    color = Column(String)
    user_id = Column(String(36), nullable=True, index=True)
    # Set when the tag is archived; NULL means active. Archiving only hides the
    # tag: its task/note/habit links are left alone.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Set when the tag is deleted (moved to the trash); NULL otherwise. Its
    # links are kept so a restore brings them back; items hide it meanwhile.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    tasks = relationship(
        "Task",
        secondary=task_tags,
        back_populates="tags"
    )
    notes = relationship(
        "Note",
        secondary=note_tags,
        back_populates="tags",
    )
    habits = relationship(
        "Habit",
        secondary=habit_tags,
        back_populates="tags",
    )
