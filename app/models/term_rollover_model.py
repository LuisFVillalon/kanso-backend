from sqlalchemy import Boolean, Column, Date, String, false

from app.database.database import Base


class TermRollover(Base):
    """How a user's term tracker rolls over when the current term ends. One row per user.

    The next term waits here until the current term (calendar_settings) ends,
    then becomes the current one. With auto_archive on, the ended term is
    archived (archived_terms) first; with it off, it is kept in the ended_*
    slot so the user can still archive it.
    """

    __tablename__ = "term_rollovers"

    user_id = Column(String(36), primary_key=True)
    auto_archive = Column(Boolean, nullable=False, default=False, server_default=false())
    next_title = Column(String, nullable=True)
    next_start_date = Column(Date, nullable=True)
    next_end_date = Column(Date, nullable=True)
    ended_title = Column(String, nullable=True)
    ended_start_date = Column(Date, nullable=True)
    ended_end_date = Column(Date, nullable=True)
