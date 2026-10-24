from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.database.database import Base
from app.models.event_tag_model import event_exception_tags, event_tags


class Event(Base):
    """A calendar event series: a one-off event, or one that repeats weekly.

    Times are local wall-clock times in the series' own time_zone, so a 10:00
    class stays at 10:00 across a DST change; occurrences are expanded on
    read (see crud/event_occurrence_crud.py), never stored. An end_time
    before start_time means the event runs past midnight into the next day.
    """

    __tablename__ = "events"
    __table_args__ = (
        # Lists and calendar reads default to undeleted rows only, so index just those per user.
        Index("ix_events_user_id_live", "user_id", postgresql_where=text("deleted_at IS NULL")),
        # The expiry purge finds rows deleted past retention by deleted_at alone.
        Index("ix_events_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
        # Clearing split_from_id when the series it was split from is purged.
        Index("ix_events_split_from_id", "split_from_id", postgresql_where=text("split_from_id IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    all_day = Column(Boolean, nullable=False, default=False)
    # The first day of the series (the only day of a one-off event), and the
    # last day it can repeat on; NULL repeats forever.
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    # Local times in time_zone; NULL for an all-day event.
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    # IANA zone the times are in, from the browser at creation.
    time_zone = Column(String(64), nullable=False)
    # Weekdays it repeats on, 0=Sun..6=Sat as Profile.rest_days; empty for a
    # one-off event.
    repeat_days = Column(JSON, nullable=False, default=list)
    # Repeats every this many weeks, counted in Sunday-to-Saturday weeks from
    # the week of start_date.
    repeat_interval = Column(Integer, nullable=False, default=1)
    # Minutes before each occurrence starts to send a reminder notification;
    # NULL sends none.
    reminder_minutes = Column(Integer, nullable=True)
    # The series this one was split from by an edit, archive or delete of
    # "this and following events".
    split_from_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    created_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Set when the whole series is archived; NULL means active.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Set when the series is deleted (moved to the trash); NULL otherwise.
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    tags = relationship("Tag", secondary=event_tags)
    exceptions = relationship(
        "EventException",
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EventException.occurrence_date",
    )


class EventException(Base):
    """One occurrence of a series that differs from the rule: edited, moved,
    archived or deleted on its own ("this event").

    Override columns left NULL take the series' value, so later edits to all
    events still reach the fields this occurrence didn't change.
    """

    __tablename__ = "event_exceptions"
    __table_args__ = (
        UniqueConstraint("event_id", "occurrence_date", name="uq_event_exceptions_event_occurrence"),
        # The expiry purge finds occurrences deleted past retention by deleted_at alone.
        Index("ix_event_exceptions_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    # The day the series' rule puts this occurrence on, which identifies it.
    occurrence_date = Column(Date, nullable=False)
    # Overrides; NULL keeps the series' value. An empty description clears it.
    date = Column(Date, nullable=True)
    title = Column(String, nullable=True)
    description = Column(String, nullable=True)
    all_day = Column(Boolean, nullable=True)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    # True when this occurrence has its own tags (event_exception_tags), even none.
    tags_overridden = Column(Boolean, nullable=False, default=False)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # Set while the occurrence is in the trash.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Deleted for good: purged from the trash or past its retention. The row
    # stays so the rule doesn't bring the occurrence back.
    cancelled = Column(Boolean, nullable=False, default=False)

    event = relationship("Event", back_populates="exceptions")
    tags = relationship("Tag", secondary=event_exception_tags)
