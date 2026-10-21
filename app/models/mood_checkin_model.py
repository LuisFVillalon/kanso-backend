from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY

from app.database.database import Base


class MoodCheckin(Base):
    """One mood check-in for a local day, week or month.

    period_start is the caller's local date: the day itself, the Monday of a
    week, or the 1st of a month. Each kind has its own optional fields (see
    schemas/mood_schema.py); the API rejects a field on the wrong kind.
    """

    __tablename__ = "mood_checkins"
    __table_args__ = (
        # One check-in per period; also serves the per-user range query.
        UniqueConstraint("user_id", "period_kind", "period_start", name="uq_mood_checkins_user_kind_start"),
        CheckConstraint("period_kind IN ('day', 'week', 'month')", name="ck_mood_checkins_period_kind"),
        CheckConstraint("mood BETWEEN 1 AND 5", name="ck_mood_checkins_mood"),
        CheckConstraint("energy BETWEEN 1 AND 5", name="ck_mood_checkins_energy"),
        CheckConstraint("stress BETWEEN 1 AND 5", name="ck_mood_checkins_stress"),
        CheckConstraint("workload BETWEEN 0 AND 2", name="ck_mood_checkins_workload"),
        CheckConstraint("progress BETWEEN 1 AND 5", name="ck_mood_checkins_progress"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), nullable=False)
    period_kind = Column(String(5), nullable=False)
    period_start = Column(Date, nullable=False)
    # 1 Rough, 2 Low, 3 Okay, 4 Good, 5 Great.
    mood = Column(SmallInteger, nullable=False)
    # Day only: 1 Drained to 5 Charged, and 1 Calm to 5 Maxed.
    energy = Column(SmallInteger, nullable=True)
    stress = Column(SmallInteger, nullable=True)
    # Week only: 0 too light, 1 about right, 2 too much; and the best part of the week.
    workload = Column(SmallInteger, nullable=True)
    highlight = Column(String(120), nullable=True)
    # Month only: progress on what matters, 1 none to 5 a lot; and the month in one word.
    progress = Column(SmallInteger, nullable=True)
    word = Column(String(24), nullable=True)
    note = Column(String(280), nullable=True)
    # Mood tags, a list of their own: never rows in the task/note/habit tags table.
    tags = Column(ARRAY(String(50)), nullable=False, server_default=text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
