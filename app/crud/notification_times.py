"""
When notifications fire: the user's local wall times turned into UTC
instants, across DST changes, and held through quiet hours.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# The frontend's defaults for a profile without these times set.
DEFAULT_DAY_START = time(8, 0)
DEFAULT_SHUTOFF = time(22, 0)


def parse_hhmm(value: str | None, default: time | None = None) -> time | None:
    """Parse a stored "HH:MM" local time, falling back to default if missing or malformed."""
    if not value:
        return default
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except ValueError:
        return default


def local_instant(day: date, t: time, tz: ZoneInfo, fold: int = 0) -> datetime:
    """The UTC instant of local wall time `t` on `day` in `tz`.

    A time skipped by a DST change resolves to the matching instant after it
    (02:30 on a spring-forward night is 03:30); a repeated one to its first
    pass unless fold=1.
    """
    return datetime.combine(day, t, tzinfo=tz).replace(fold=fold).astimezone(UTC)


def local_midnight_after(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant the local day after `day` starts."""
    return local_instant(day + timedelta(days=1), time.min, tz)


def hold_for_quiet_hours(fire_at: datetime, quiet: tuple[time, time] | None, tz: ZoneInfo) -> datetime:
    """When an alert firing at fire_at is delivered: at once, or when the quiet hours around it end.

    Quiet hours with start after end run past midnight (22:00 to 07:00).
    """
    if quiet is None:
        return fire_at
    start, end = quiet
    local = fire_at.astimezone(tz)
    wall, day = local.time(), local.date()
    if start < end:
        if not start <= wall < end:
            return fire_at
    elif wall >= start:
        day += timedelta(days=1)
    elif wall >= end:
        return fire_at
    # On a fall-back night the end's first pass can already be behind fire_at.
    for fold in (0, 1):
        held = local_instant(day, end, tz, fold)
        if held > fire_at:
            return held
    return fire_at


def shutoff_at(day: date, shutoff: time, day_start: time, tz: ZoneInfo) -> datetime:
    """When `day` is called a day. A "Call it a day" at or before the day start falls after midnight."""
    return local_instant(day + timedelta(days=1) if shutoff <= day_start else day, shutoff, tz)
