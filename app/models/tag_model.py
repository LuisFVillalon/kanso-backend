from sqlalchemy import Column, Integer, String, UniqueConstraint
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
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    color = Column(String)
    user_id = Column(String(36), nullable=True, index=True)

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
