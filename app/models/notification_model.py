from sqlalchemy import JSON, Column, Date, DateTime, Index, Integer, String

from app.database.database import Base


class NotificationPreferences(Base):
    __tablename__ = "notification_preferences"

    user_id = Column(String(36), primary_key=True)
    # Per-type on/off overrides, e.g. {"overdue": false}. A type left out is
    # on, so a new alert type needs no backfill of existing rows.
    types = Column(JSON, nullable=True)
    # Local "HH:MM" bounds, both set or both NULL. Alerts that fire inside
    # them are held until the end.
    quiet_hours_start = Column(String(5), nullable=True)
    quiet_hours_end = Column(String(5), nullable=True)
    # Minutes before a task's due time to remind; NULL means the built-in default.
    default_reminder_minutes = Column(Integer, nullable=True)
    # Minutes before "Call it a day" to remind about the mood check-in, 30 or
    # 60; NULL means the built-in default.
    mood_reminder_minutes = Column(Integer, nullable=True)


class NotificationState(Base):
    """What the user did with one notification. Notifications themselves are
    computed on demand (see crud/notification_crud.py); only this is stored."""

    __tablename__ = "notification_states"
    __table_args__ = (
        # The lazy prune finds rows for old occurrences across every user.
        Index("ix_notification_states_occurs_on", "occurs_on"),
    )

    user_id = Column(String(36), primary_key=True)
    # The notification's deterministic "{type}:{subject}:{occurrence}" id.
    notification_id = Column(String(100), primary_key=True)
    # The occurrence's local date, parsed from the id.
    occurs_on = Column(Date, nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
