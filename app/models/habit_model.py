from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.habit_tag_model import habit_tags


class Habit(Base):
    __tablename__ = "habits"
    __table_args__ = (
        # Lists default to active, undeleted rows only, so index just those per user.
        Index(
            "ix_habits_user_id_active", "user_id", postgresql_where=text("archived_at IS NULL AND deleted_at IS NULL")
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    user_id = Column(String(36), nullable=True, index=True)
    current_streak = Column(Integer, nullable=False, default=0)
    max_streak = Column(Integer, nullable=False, default=0)
    estimated_time = Column(Numeric(precision=10, scale=2), nullable=True)
    # Set when the habit is archived; NULL means active.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Set when the habit is deleted (moved to the trash); NULL otherwise.
    # Its logs are kept, so a restore brings back its history.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

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
