from sqlalchemy import JSON, Boolean, Column, DateTime, String, func

from app.database.database import Base


class Profile(Base):
    __tablename__ = "profiles"

    user_id = Column(String(36), primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    shutoff_time = Column(String(5), nullable=True)
    avatar = Column(String, nullable=True)
    theme_accent = Column(String, nullable=True)
    page_style = Column(String, nullable=True)
    day_start_time = Column(String(5), nullable=True)
    rest_days = Column(JSON, nullable=True)
    layout_order = Column(JSON, nullable=True)
    layout_sizes = Column(JSON, nullable=True)
    app_mode = Column(String, nullable=True)
    daily_brief_collapsed = Column(Boolean, nullable=True)
    dashboard_view = Column(String, nullable=True)
    notes_view_mode = Column(String, nullable=True)
    home_panels = Column(String, nullable=True)
    # Mood check-in settings the user has changed; NULL or a missing key means
    # that setting's default (see schemas/mood_schema.py:MoodCheckinSettings).
    mood_checkins = Column(JSON, nullable=True)
