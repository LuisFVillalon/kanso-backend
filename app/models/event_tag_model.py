from sqlalchemy import Column, ForeignKey, Integer, Table

from app.database.database import Base

event_tags = Table(
    "event_tags",
    Base.metadata,
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

# The tags of one edited occurrence, used only when its exception has tags_overridden.
event_exception_tags = Table(
    "event_exception_tags",
    Base.metadata,
    Column("exception_id", Integer, ForeignKey("event_exceptions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
