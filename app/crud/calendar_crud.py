from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models.calendar_settings_model import CalendarSettings as CalendarSettingsModel


def get_calendar_settings(db: Session, user_id: str) -> CalendarSettingsModel | None:
    return (
        db.query(CalendarSettingsModel)
        .filter(CalendarSettingsModel.user_id == user_id)
        .first()
    )


def _default_term() -> tuple[date, date]:
    """The current month plus the next two — mirrors defaultTermSettings() on the frontend."""
    today = date.today()
    start = today.replace(day=1)
    month_after = start.month + 3
    year = start.year + (month_after - 1) // 12
    end = date(year, (month_after - 1) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def upsert_calendar_settings(
    db: Session, user_id: str, changes: dict
) -> CalendarSettingsModel:
    row = get_calendar_settings(db, user_id)
    if row is None:
        default_start, default_end = _default_term()
        row = CalendarSettingsModel(
            user_id=user_id,
            title=changes.get("title", "Term Tracker"),
            start_date=changes.get("start_date", default_start),
            end_date=changes.get("end_date", default_end),
        )
        db.add(row)
    else:
        for key, val in changes.items():
            setattr(row, key, val)
    db.commit()
    db.refresh(row)
    return row
