from sqlalchemy import Column, ForeignKey, Integer, Table

from app.database.database import Base

habit_tags = Table(
    "habit_tags",
    Base.metadata,
    Column("habit_id", Integer, ForeignKey("habits.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
