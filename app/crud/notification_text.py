"""Wording for notification titles and bodies: clock times, dates, countdowns and short lists."""

from datetime import date, datetime, time, timedelta


def clock(t: time) -> str:
    """"4:00 PM"."""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def day_label(day: date) -> str:
    """"Wed, Oct 21"."""
    return f"{day:%a, %b} {day.day}"


def relative_when(due_local: datetime, from_local: datetime) -> str:
    """"today at 4:00 PM", "tomorrow at 9:00 AM" or "on Wed, Oct 21 at 9:00 AM", as seen from from_local."""
    at = clock(due_local.time())
    days = (due_local.date() - from_local.date()).days
    if days == 0:
        return f"today at {at}"
    if days == 1:
        return f"tomorrow at {at}"
    return f"on {day_label(due_local.date())} at {at}"


def duration(delta: timedelta) -> str:
    """A countdown such as "2 hours", "1 hour 30 minutes" or "1 day", rounded to the minute."""
    minutes = max(1, round(delta.total_seconds() / 60))
    parts = []
    for size, unit in ((24 * 60, "day"), (60, "hour"), (1, "minute")):
        count, minutes = divmod(minutes, size)
        if count:
            parts.append(f"{count} {unit}{'' if count == 1 else 's'}")
    return " ".join(parts[:2])


def list_days(days: list[date]) -> str:
    """"Oct 19", "Oct 19 and Oct 20" or "Oct 18, Oct 19 and Oct 20"."""
    labels = [f"{day:%b} {day.day}" for day in days]
    return " and ".join(filter(None, (", ".join(labels[:-1]), labels[-1])))


def count_of(n: int, noun: str) -> str:
    """"1 task", "3 tasks"."""
    return f"{n} {noun}{'' if n == 1 else 's'}"
