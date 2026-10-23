from sqlalchemy import Column, Date, DateTime, Integer, String, func

from app.database.database import Base


class ArchivedTerm(Base):
    """A past term tracker the user archived, kept so the month calendar can still show it.

    Archiving moves the current term (calendar_settings) here, leaving the
    user free to set up a new one. A user can have any number of these.
    """

    __tablename__ = "archived_terms"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), nullable=False, index=True)
    title = Column(String, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    archived_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
