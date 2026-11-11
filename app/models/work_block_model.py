from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, String, Time, func, text
from sqlalchemy.orm import relationship

from app.database.database import Base


class WorkBlock(Base):
    """A planned "work on it" time on the calendar, for a task or a habit.

    Not an event: Reports and event reminders never count it. Exactly one of
    task_id and habit_id is set, and deleting that task or habit for good
    deletes its blocks. Times are local wall-clock times in time_zone; an
    end_time before start_time runs past midnight into the next day.
    starts_at and ends_at are the same span as UTC instants, kept in step
    with them on every write.
    """

    __tablename__ = "work_blocks"
    __table_args__ = (
        CheckConstraint("(task_id IS NULL) <> (habit_id IS NULL)", name="ck_work_blocks_task_xor_habit"),
        # Calendar and debrief reads take a user's blocks overlapping a span.
        Index("ix_work_blocks_user_id_starts_at", "user_id", "starts_at"),
        # Dropping a task's future blocks, and the FK cascades.
        Index("ix_work_blocks_task_id", "task_id", postgresql_where=text("task_id IS NOT NULL")),
        Index("ix_work_blocks_habit_id", "habit_id", postgresql_where=text("habit_id IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True)
    habit_id = Column(Integer, ForeignKey("habits.id", ondelete="CASCADE"), nullable=True)
    # The local day it starts on, and its local times, in time_zone.
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    time_zone = Column(String(64), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    created_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Many-to-one only: the database cascade removes blocks with their task or
    # habit, so neither parent needs (or loads) a collection of them.
    task = relationship("Task")
    habit = relationship("Habit")
