from sqlalchemy import Column, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.habit_tag_model import habit_tags


class Habit(Base):
    __tablename__ = "habits"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    user_id = Column(String(36), nullable=True, index=True)
    current_streak = Column(Integer, nullable=False, default=0)
    max_streak = Column(Integer, nullable=False, default=0)
    estimated_time = Column(Numeric(precision=10, scale=2), nullable=True)

    tags = relationship(
        "Tag",
        secondary=habit_tags,
        back_populates="habits",
    )
    logs = relationship(
        "HabitLog",
        backref="habit",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
