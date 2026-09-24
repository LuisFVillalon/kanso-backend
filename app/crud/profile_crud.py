from sqlalchemy.orm import Session

from app.models.profile_model import Profile
from app.schemas.profile_schema import ProfileSave


def get_profile(db: Session, user_id: str) -> Profile | None:
    return db.query(Profile).filter(Profile.user_id == user_id).first()


def upsert_profile(db: Session, user_id: str, body: ProfileSave) -> Profile:
    fields = body.model_dump(exclude_unset=True)
    profile = get_profile(db, user_id)
    if profile is None:
        profile = Profile(user_id=user_id, **fields)
        db.add(profile)
    else:
        for key, value in fields.items():
            setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile
