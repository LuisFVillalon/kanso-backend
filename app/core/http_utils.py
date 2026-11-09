import os
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status

# Comma-separated list of allowed frontend origins, e.g.
# "http://localhost:3000,https://kanso-web-app.vercel.app"
# New deployment targets (a staging URL, a custom domain) are added via env
# var, not a source change.
_DEFAULT_ORIGINS = "http://localhost:3000,https://kanso-web-app.vercel.app"


def allowed_origins() -> list[str]:
    return [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if origin.strip()]


def default_origin() -> str:
    """The frontend to send a browser to when the request doesn't say which: the first https one."""
    origins = allowed_origins()
    return ([origin for origin in origins if origin.startswith("https://")] or origins or [""])[0]


def require_found(obj, detail: str = "Not found"):
    """Raise 404 if obj is None, otherwise return it."""
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return obj


def require_time_zone(tz: str) -> ZoneInfo:
    """Resolve a caller-supplied IANA time zone name, raising 422 if it isn't one."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Unknown time zone: {tz!r}."
        ) from None


def parse_iso_date(value: str) -> date:
    """Parse a caller-supplied "YYYY-MM-DD" date, raising 400 if it isn't one."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date format. Use YYYY-MM-DD."
        ) from None


def require_date_range(start: date, end: date, max_days: int | None = None) -> None:
    """Raise 400 if start is after end, or the inclusive range spans more than max_days days."""
    if start > end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start_date must not be after end_date.")
    if max_days is not None and (end - start).days + 1 > max_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The range may span at most {max_days} days.",
        )
