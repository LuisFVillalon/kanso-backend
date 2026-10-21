from sqlalchemy.orm import Session

from app.models.calendar_settings_model import CalendarSettings
from app.models.canvas_connection_model import CanvasConnection
from app.models.drawing_model import Drawing
from app.models.habit_model import Habit
from app.models.mood_checkin_model import MoodCheckin
from app.models.note_model import Note
from app.models.notification_model import NotificationPreferences, NotificationState
from app.models.profile_model import Profile
from app.models.tag_model import Tag
from app.models.task_model import Task

# Every table holding rows scoped to a user_id. Adding a new user-owned table?
# Add its model here — account deletion and the demo reset both rely on it.
# note_sessions, habit_logs and the task/note/habit tag join tables cascade
# from their parents at the database level.
USER_OWNED_MODELS = (
    Note, Task, Habit, Tag, Profile, Drawing, CalendarSettings, NotificationPreferences, NotificationState,
    MoodCheckin,
    CanvasConnection,
)


def delete_user_rows(db: Session, user_id: str) -> None:
    """Delete every row owned by user_id. Flushes but does not commit."""
    for model in USER_OWNED_MODELS:
        db.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
    db.flush()
