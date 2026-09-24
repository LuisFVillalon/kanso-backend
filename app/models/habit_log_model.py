from sqlalchemy import Column, Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.sql import func

from app.database.database import Base


class HabitLog(Base):
    __tablename__ = "habit_logs"

    id = Column(Integer, primary_key=True, index=True)
    habit_id = Column(Integer, ForeignKey("habits.id", ondelete="CASCADE"), nullable=False, index=True)
    logged_date = Column(Date, nullable=False, server_default=func.current_date())

    __table_args__ = (
        UniqueConstraint("habit_id", "logged_date", name="uq_habit_logs_habit_date"),
    )
