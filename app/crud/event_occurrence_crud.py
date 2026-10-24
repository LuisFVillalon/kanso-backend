"""
Event occurrences, expanded on read from each series' rule.

Nothing per occurrence is stored except an exception row for one that was
edited, moved, archived or deleted on its own. A series repeats on its
repeat_days every repeat_interval weeks (Sunday-to-Saturday weeks counted
from the week of start_date) through end_date, at local wall times in its
own time zone, so it keeps its clock time across DST changes.

What the calendar shows:

- Deleted series and deleted occurrences are left out.
- Past occurrences (ended before now) are shown, archived ones included and
  flagged, so the calendar keeps its history.
- Archived occurrences that haven't ended yet are left out; they are listed
  under the archive instead (get_archived_occurrences).
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, selectinload

from app.crud.profile_crud import get_profile, work_window
from app.models.event_model import Event, EventException
from app.models.profile_model import Profile
from app.schemas.event_schema import (
    EventOccurrence,
    EventReport,
    EventReportDay,
    EventReportPeriod,
    EventTagMinutes,
)

DAY = timedelta(days=1)


# ── The rule ─────────────────────────────────────────────────────────────────

def week_start(day: date) -> date:
    """The Sunday starting `day`'s week."""
    return day - timedelta(days=day.isoweekday() % 7)


def occurs_on(event: Event, day: date) -> bool:
    """Whether the series' rule puts an occurrence on `day`, exceptions aside."""
    if not event.repeat_days:
        return day == event.start_date
    if day < event.start_date or (event.end_date is not None and day > event.end_date):
        return False
    if day.isoweekday() % 7 not in event.repeat_days:
        return False
    weeks = (week_start(day) - week_start(event.start_date)).days // 7
    return weeks % event.repeat_interval == 0


def rule_dates(event: Event, first: date, last: date) -> Iterator[date]:
    """The days in first..last the series' rule puts an occurrence on."""
    day = max(first, event.start_date)
    if event.end_date is not None:
        last = min(last, event.end_date)
    if not event.repeat_days:
        last = min(last, event.start_date)
    while day <= last:
        if occurs_on(event, day):
            yield day
        day += DAY


# ── Occurrences ──────────────────────────────────────────────────────────────

def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def _instant(day: date, t: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of local wall time `t` on `day`. A time skipped by a DST
    change resolves to the matching instant after it (02:30 is 03:30)."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


@dataclass(eq=False)
class Occurrence:
    event: Event
    exception: EventException | None
    occurrence_date: date
    # The day it is on in the series' time zone, after any move.
    day: date
    title: str
    description: str | None
    all_day: bool
    start_time: time | None
    end_time: time | None
    tags: list
    starts_at: datetime
    ends_at: datetime
    archived: bool
    edited: bool

    def past(self, now: datetime) -> bool:
        return self.ends_at <= now

    def visible(self, now: datetime) -> bool:
        """Shown on calendars: archived occurrences only once they're over."""
        return not self.archived or self.past(now)

    def minutes(self, start: datetime | None = None, end: datetime | None = None) -> float:
        """Minutes of the occurrence inside [start, end), or all of it. All-day events have none."""
        if self.all_day:
            return 0
        return overlap_minutes(self.starts_at, self.ends_at, start or self.starts_at, end or self.ends_at)


def _pick(override, inherited):
    return inherited if override is None else override


def build_occurrence(event: Event, occurrence_date: date, exception: EventException | None) -> Occurrence:
    """The occurrence the rule puts on occurrence_date, with its exception's overrides."""
    tz = ZoneInfo(event.time_zone)
    exc = exception
    day = (exc.date if exc else None) or occurrence_date
    all_day = _pick(exc.all_day if exc else None, event.all_day)
    start_time = _pick(exc.start_time if exc else None, event.start_time)
    end_time = _pick(exc.end_time if exc else None, event.end_time)
    if all_day:
        start_time = end_time = None
        starts_at, ends_at = _midnight(day, tz), _midnight(day + DAY, tz)
    else:
        starts_at = _instant(day, start_time, tz)
        ends_at = _instant(day + DAY if end_time <= start_time else day, end_time, tz)
    description = _pick(exc.description if exc else None, event.description)
    edited = exc is not None and (
        exc.tags_overridden
        or any(
            getattr(exc, field) is not None
            for field in ("date", "title", "description", "all_day", "start_time", "end_time")
        )
    )
    return Occurrence(
        event=event,
        exception=exc,
        occurrence_date=occurrence_date,
        day=day,
        title=_pick(exc.title if exc else None, event.title),
        description=description or None,
        all_day=all_day,
        start_time=start_time,
        end_time=end_time,
        tags=list(exc.tags if exc and exc.tags_overridden else event.tags),
        starts_at=starts_at,
        ends_at=ends_at,
        archived=event.archived_at is not None or (exc is not None and exc.archived_at is not None),
        edited=edited,
    )


def _gone(exc: EventException | None) -> bool:
    return exc is not None and (exc.cancelled or exc.deleted_at is not None)


def expand(event: Event, first: date, last: date, tz: ZoneInfo | None) -> Iterator[Occurrence]:
    """The series' live occurrences that fall on local days first..last in `tz`
    (the series' own zone if None), deleted ones left out.

    A timed occurrence counts if any of it falls in the range, so one running
    past midnight shows on both days. An all-day occurrence keeps its day.
    """
    view_tz = tz or ZoneInfo(event.time_zone)
    range_start, range_end = _midnight(first, view_tz), _midnight(last + DAY, view_tz)
    exceptions = {exc.occurrence_date: exc for exc in event.exceptions}

    def in_range(occ: Occurrence) -> bool:
        if occ.all_day:
            return first <= occ.day <= last
        return occ.starts_at < range_end and occ.ends_at > range_start

    # Days in the series' zone can sit a day off the caller's, and an
    # occurrence past midnight reaches into the next day.
    for day in rule_dates(event, first - 2 * DAY, last + 2 * DAY):
        exc = exceptions.get(day)
        if _gone(exc) or (exc is not None and exc.date is not None):
            continue
        occ = build_occurrence(event, day, exc)
        if in_range(occ):
            yield occ
    # Moved occurrences, wherever the rule put them.
    for exc in exceptions.values():
        if exc.date is None or _gone(exc) or not occurs_on(event, exc.occurrence_date):
            continue
        occ = build_occurrence(event, exc.occurrence_date, exc)
        if in_range(occ):
            yield occ


def load_series(db: Session, user_id: str) -> list[Event]:
    """The user's undeleted event series, with tags and exceptions loaded."""
    return (
        db.query(Event)
        .options(
            selectinload(Event.tags),
            selectinload(Event.exceptions).selectinload(EventException.tags),
        )
        .filter(Event.user_id == user_id, Event.deleted_at.is_(None))
        .order_by(Event.id)
        .all()
    )


def occurrences_between(db: Session, user_id: str, first: date, last: date, tz: ZoneInfo | None) -> list[Occurrence]:
    """Every live occurrence on local days first..last, archived ones included, in start order."""
    occurrences = [occ for event in load_series(db, user_id) for occ in expand(event, first, last, tz)]
    return sorted(occurrences, key=lambda o: (o.starts_at, o.event.id, o.occurrence_date))


def present(occ: Occurrence, tz: ZoneInfo | None, now: datetime) -> EventOccurrence:
    """An occurrence as placed on the caller's calendar in `tz` (the series' own zone if None)."""
    event = occ.event
    if occ.all_day:
        day, end_day, start_time, end_time = occ.day, occ.day, None, None
    else:
        view_tz = tz or ZoneInfo(event.time_zone)
        local_start, local_end = occ.starts_at.astimezone(view_tz), occ.ends_at.astimezone(view_tz)
        day, start_time = local_start.date(), local_start.time()
        end_day, end_time = local_end.date(), local_end.time()
    return EventOccurrence(
        event_id=event.id,
        occurrence_date=occ.occurrence_date,
        title=occ.title,
        description=occ.description,
        tags=occ.tags,
        all_day=occ.all_day,
        date=day,
        end_date=end_day,
        start_time=start_time,
        end_time=end_time,
        starts_at=occ.starts_at,
        ends_at=occ.ends_at,
        time_zone=event.time_zone,
        repeat_days=event.repeat_days or [],
        repeat_interval=event.repeat_interval,
        reminder_minutes=event.reminder_minutes,
        edited=occ.edited,
        past=occ.past(now),
        archived=occ.archived,
    )


def get_occurrences(
    db: Session, user_id: str, first: date, last: date, tz: ZoneInfo, now: datetime
) -> list[EventOccurrence]:
    """The occurrences a calendar shows on local days first..last: past ones,
    archived or not, and upcoming ones that aren't archived."""
    return [
        present(occ, tz, now)
        for occ in occurrences_between(db, user_id, first, last, tz)
        if occ.visible(now)
    ]


def get_archived_occurrences(db: Session, user_id: str, tz: ZoneInfo, now: datetime) -> list[EventOccurrence]:
    """Occurrences archived on their own, of series that are neither archived nor deleted, by start.

    Whole archived series are listed by GET /get-events?archived=true instead.
    """
    occurrences = [
        build_occurrence(event, exc.occurrence_date, exc)
        for event in load_series(db, user_id)
        if event.archived_at is None
        for exc in event.exceptions
        if exc.archived_at is not None and not _gone(exc) and occurs_on(event, exc.occurrence_date)
    ]
    occurrences.sort(key=lambda o: (o.starts_at, o.event.id))
    return [present(occ, tz, now) for occ in occurrences]


# ── Time arithmetic ──────────────────────────────────────────────────────────

def overlap_minutes(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> float:
    return max(0.0, (min(end, window_end) - max(start, window_start)).total_seconds() / 60)


def union_minutes(spans: Iterable[tuple[datetime, datetime]], window_start: datetime, window_end: datetime) -> float:
    """Minutes of [window_start, window_end) covered by any span; overlaps count once."""
    clipped = sorted(
        (max(s, window_start), min(e, window_end)) for s, e in spans if s < window_end and e > window_start
    )
    total, covered_to = 0.0, None
    for s, e in clipped:
        if covered_to is not None and s < covered_to:
            s = covered_to
        if e > s:
            total += (e - s).total_seconds() / 60
            covered_to = e
    return total


def window_span(profile: Profile | None, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """`day`'s work window as UTC instants; one ending at or before its start ends the next day."""
    start, end = work_window(profile, day)
    return _instant(day, start, tz), _instant(day + DAY if end <= start else day, end, tz)


def is_rest_day(profile: Profile | None, day: date) -> bool:
    return day.isoweekday() % 7 in ((profile.rest_days if profile else None) or [])  # 0=Sun..6=Sat


def minutes_by_tag(
    occurrences: Iterable[Occurrence], minutes_of: Callable[[Occurrence], float]
) -> list[EventTagMinutes]:
    """Each tag's event minutes (minutes_of each occurrence) and how many events carry it.

    An event with several tags counts in full under each; untagged events
    are under tag null. Largest first.
    """
    buckets: dict[int | None, EventTagMinutes] = {}
    for occ in occurrences:
        minutes = minutes_of(occ)
        tags = [t for t in occ.tags if t.deleted_at is None] or [None]
        for tag in tags:
            bucket = buckets.setdefault(tag.id if tag else None, EventTagMinutes(tag=tag))
            bucket.minutes += minutes
            bucket.count += 1
    return sorted(buckets.values(), key=lambda b: (-b.minutes, -b.count, b.tag is None, b.tag.name if b.tag else ""))


# ── Reports ──────────────────────────────────────────────────────────────────

def get_event_report(db: Session, user_id: str, first: date, last: date, tz: ZoneInfo, now: datetime) -> EventReport:
    """Event time over local days first..last: by tag, day, week and month, and
    how much of the work windows events left free.

    Counts what the calendar shows, so past archived occurrences are in and
    upcoming archived ones are not. A day's work window is its profile
    day_windows entry or the day start and "Call it a day" times (8 AM to
    10 PM if unset); rest days have none.
    """
    profile = get_profile(db, user_id)
    # A work window past midnight reaches into the day after the range.
    occurrences = [occ for occ in occurrences_between(db, user_id, first, last + DAY, tz) if occ.visible(now)]
    timed = [occ for occ in occurrences if not occ.all_day]

    days: list[EventReportDay] = []
    counted: list[Occurrence] = []
    day = first
    while day <= last:
        day_start, day_end = _midnight(day, tz), _midnight(day + DAY, tz)
        starting = [
            occ for occ in occurrences
            if (occ.day if occ.all_day else occ.starts_at.astimezone(tz).date()) == day
        ]
        counted += starting
        report_day = EventReportDay(
            date=day,
            count=len(starting),
            minutes=sum(occ.minutes(day_start, day_end) for occ in timed),
            is_rest_day=is_rest_day(profile, day),
        )
        if not report_day.is_rest_day:
            window_start, window_end = window_span(profile, day, tz)
            report_day.window_minutes = (window_end - window_start).total_seconds() / 60
            report_day.event_window_minutes = union_minutes(
                ((occ.starts_at, occ.ends_at) for occ in timed), window_start, window_end
            )
            report_day.free_minutes = report_day.window_minutes - report_day.event_window_minutes
        days.append(report_day)
        day += DAY

    weeks: dict[date, EventReportPeriod] = {}
    months: dict[date, EventReportPeriod] = {}
    for report_day in days:
        for periods, key in ((weeks, week_start(report_day.date)), (months, report_day.date.replace(day=1))):
            period = periods.setdefault(key, EventReportPeriod(start=key))
            period.count += report_day.count
            period.minutes += report_day.minutes

    window_minutes = sum(d.window_minutes for d in days)
    event_window_minutes = sum(d.event_window_minutes for d in days)
    busiest = max(days, key=lambda d: d.minutes, default=None)
    range_start, range_end = _midnight(first, tz), _midnight(last + DAY, tz)
    return EventReport(
        start_date=first,
        end_date=last,
        event_count=len(counted),
        archived_count=sum(1 for occ in counted if occ.archived),
        total_minutes=sum(d.minutes for d in days),
        window_minutes=window_minutes,
        event_window_minutes=event_window_minutes,
        free_minutes=window_minutes - event_window_minutes,
        free_pct=round((window_minutes - event_window_minutes) / window_minutes * 100, 1) if window_minutes else None,
        busiest_day=busiest if busiest and busiest.minutes > 0 else None,
        by_tag=minutes_by_tag(
            [
                occ for occ in occurrences
                if (occ.day <= last if occ.all_day else occ.starts_at < range_end and occ.ends_at > range_start)
            ],
            lambda occ: occ.minutes(range_start, range_end),
        ),
        by_day=days,
        by_week=list(weeks.values()),
        by_month=list(months.values()),
    )


# ── Daily brief ──────────────────────────────────────────────────────────────

@dataclass
class TodayEvents:
    """Today's events for the daily brief."""

    shown: list[EventOccurrence]
    passed: list[EventOccurrence]
    minutes_by_tag: list[EventTagMinutes]
    passed_minutes_by_tag: list[EventTagMinutes]
    # Minutes of today's work window events take; overlaps count once.
    window_minutes_taken: float


def todays_events(
    db: Session,
    user_id: str,
    today: date,
    local_now: time | None,
    tz: ZoneInfo | None,
    window: tuple[time, time] | None,
) -> TodayEvents:
    """Today's occurrences in the caller's zone `tz` (each series' own if None).

    Archived occurrences are shown like on the calendar but count toward
    nothing: an archived event isn't happening. An event has passed once it
    ended by `local_now`; without a local time none has.
    """
    now = datetime.now(UTC)
    occurrences = [occ for event in load_series(db, user_id) for occ in expand(event, today, today, tz)]
    occurrences.sort(key=lambda o: (o.starts_at, o.event.id, o.occurrence_date))
    occurrences = [occ for occ in occurrences if occ.visible(now)]

    def zone(occ: Occurrence) -> ZoneInfo:
        return tz or ZoneInfo(occ.event.time_zone)

    def local_end(occ: Occurrence) -> datetime:
        return occ.ends_at.astimezone(zone(occ)).replace(tzinfo=None)

    def minutes_today(occ: Occurrence) -> float:
        return occ.minutes(_midnight(today, zone(occ)), _midnight(today + DAY, zone(occ)))

    happening = [occ for occ in occurrences if not occ.archived]
    passed = [
        occ for occ in happening
        if local_now is not None and local_end(occ) <= datetime.combine(today, local_now)
    ]

    taken = 0.0
    if window is not None:
        # Wall-clock arithmetic in each occurrence's zone, as the window is a wall-clock span.
        start, end = window
        window_start = datetime.combine(today, start)
        window_end = datetime.combine(today + DAY if end <= start else today, end)
        taken = union_minutes(
            (
                (occ.starts_at.astimezone(zone(occ)).replace(tzinfo=None), local_end(occ))
                for occ in happening if not occ.all_day
            ),
            window_start,
            window_end,
        )

    return TodayEvents(
        shown=[present(occ, tz, now) for occ in occurrences],
        passed=[present(occ, tz, now) for occ in passed],
        minutes_by_tag=minutes_by_tag(happening, minutes_today),
        passed_minutes_by_tag=minutes_by_tag(passed, minutes_today),
        window_minutes_taken=taken,
    )
