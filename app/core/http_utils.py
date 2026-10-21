from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status


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
