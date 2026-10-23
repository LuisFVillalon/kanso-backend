from datetime import date

from sqlalchemy.orm import Session

from app.models.profile_model import Profile
from app.schemas.mood_schema import merge_mood_settings
from app.schemas.profile_schema import ProfileSave


def get_profile(db: Session, user_id: str) -> Profile | None:
    return db.query(Profile).filter(Profile.user_id == user_id).first()


def day_window(profile: Profile | None, day: date) -> tuple[str | None, str | None]:
    """The ("HH:MM" start, "HH:MM" end) of `day`'s work window: its weekday's
    day_windows entry, or day_start_time and shutoff_time. Either can be None."""
    weekday = str(day.isoweekday() % 7)  # 0=Sun..6=Sat, matches Profile.rest_days
    window = ((profile.day_windows if profile else None) or {}).get(weekday)
    if window:
        return window["start"], window["end"]
    return (profile.day_start_time, profile.shutoff_time) if profile else (None, None)


def upsert_profile(db: Session, user_id: str, body: ProfileSave) -> Profile:
    fields = body.model_dump(exclude_unset=True)
    profile = get_profile(db, user_id)
    if fields.get("mood_checkins") is not None:
        stored = profile.mood_checkins if profile else None
        fields["mood_checkins"] = merge_mood_settings(stored, fields["mood_checkins"])
    if profile is None:
        profile = Profile(user_id=user_id, **fields)
        db.add(profile)
    else:
        for key, value in fields.items():
            setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile
