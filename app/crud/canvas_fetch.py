"""
Fetchers for the Canvas panel: one per route, each asking Canvas for what
the route shows and returning normalized shapes (see canvas_normalize).

Each fetch_* builds a callable that canvas_data_crud.read() runs with a
CanvasClient, only when the cache can't answer. fetch_feed reads a calendar
feed connection's items instead, which feed_items and feed_upcoming date.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.canvas_client import CanvasClient
from app.core.canvas_feed import CanvasFeed
from app.crud.canvas_normalize import (
    as_int,
    feed_entry,
    parse_announcement,
    parse_assignment,
    parse_course,
    parse_missing_item,
    parse_module,
    parse_planner_item,
)
from app.models.canvas_connection_model import CanvasConnection
from app.schemas.canvas_schema import (
    CanvasAnnouncement,
    CanvasAssignment,
    CanvasCourse,
    CanvasFeedItem,
    CanvasItem,
    CanvasModule,
)

UPCOMING_DAYS = 7
RECENTLY_GRADED_DAYS = 7
RECENTLY_GRADED_MAX = 10
ANNOUNCEMENT_DAYS = 14
# Modules whose items Canvas leaves out (long ones) are fetched one by one, up to this many.
MODULE_ITEM_FETCHES_MAX = 10


def upcoming_window(today: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The user's local day today through UPCOMING_DAYS days later, as aware datetimes."""
    start = datetime.combine(today, time(), tzinfo=tz)
    return start, datetime.combine(today + timedelta(days=UPCOMING_DAYS), time(), tzinfo=tz)


def fetch_overview(start: datetime, end: datetime) -> Callable[[CanvasClient], dict]:
    def fetch(client: CanvasClient) -> dict:
        planner = client.get_list(
            "/planner/items",
            {
                "start_date": (start - timedelta(days=RECENTLY_GRADED_DAYS)).isoformat(),
                "end_date": end.isoformat(),
                "per_page": 100,
            },
        )
        missing = client.get_list(
            "/users/self/missing_submissions",
            {"include[]": "course", "filter[]": ["submittable", "current_grading_period"], "per_page": 100},
        )
        items = [item for raw in planner if (item := parse_planner_item(client.base_url, raw)) is not None]
        upcoming = [item for item in items if item.due_at is None or start <= item.due_at < end]
        upcoming.sort(key=lambda item: (item.due_at is None, item.due_at or end, item.id))
        missing_items = [item for raw in missing if (item := parse_missing_item(client.base_url, raw)) is not None]
        missing_items.sort(key=lambda item: (item.due_at is None, item.due_at or end, item.id))
        return {"upcoming": upcoming, "missing": missing_items, "recently_graded": recently_graded(items, start)}

    return fetch


@dataclass(frozen=True)
class FeedEntry:
    """A calendar feed item as cached, before the caller's time zone is known."""

    # due_at is None for an all-day item.
    item: CanvasFeedItem
    # The day an all-day item falls on.
    day: date | None = None


def fetch_feed(feed: CanvasFeed) -> list[FeedEntry]:
    return [FeedEntry(*feed_entry(feed.base_url, event)) for event in feed.events()]


def feed_zone(connection: CanvasConnection) -> ZoneInfo:
    """The zone a feed's all-day items are due in: the one stored when it was connected, else UTC."""
    return ZoneInfo(connection.time_zone or "UTC")


def feed_items(entries: list[FeedEntry], tz: ZoneInfo) -> list[CanvasFeedItem]:
    """The feed's items with every due_at set: an all-day assignment is due at 11:59:59 pm in tz, anything else at 0:00.

    tz is the connection's feed_zone, so the overview and the sync give the same due_at whatever the caller's zone.
    """
    items = []
    for entry in entries:
        if entry.day is None:
            items.append(entry.item)
            continue
        at = time(23, 59, 59) if entry.item.kind == "assignment" else time()
        due_at = datetime.combine(entry.day, at, tzinfo=tz)
        items.append(entry.item.model_copy(update={"due_at": due_at, "day": entry.day}))
    return items


def feed_upcoming(entries: list[FeedEntry], tz: ZoneInfo, start: datetime, end: datetime) -> list[CanvasFeedItem]:
    """The feed's items due in [start, end), soonest first, like the overview's upcoming list. tz as for feed_items."""
    upcoming = [item for item in feed_items(entries, tz) if start <= item.due_at < end]
    upcoming.sort(key=lambda item: (item.due_at, item.id))
    return upcoming


def recently_graded(items: list[CanvasItem], start: datetime) -> list[CanvasItem]:
    """Graded items due in the RECENTLY_GRADED_DAYS before start, newest first, at most RECENTLY_GRADED_MAX."""
    since = start - timedelta(days=RECENTLY_GRADED_DAYS)
    graded = [
        item for item in items
        if item.submission is not None and item.submission.graded
        and item.due_at is not None and since <= item.due_at < start
    ]
    graded.sort(key=lambda item: (item.due_at, item.id), reverse=True)
    return graded[:RECENTLY_GRADED_MAX]


def fetch_courses(client: CanvasClient) -> list[CanvasCourse]:
    courses = client.get_list(
        "/courses",
        {
            "enrollment_state": "active",
            "include[]": ["total_scores", "current_grading_period_scores", "term"],
            "per_page": 100,
        },
    )
    return [course for raw in courses if (course := parse_course(client.base_url, raw)) is not None]


def fetch_modules(course_id: int) -> Callable[[CanvasClient], list[CanvasModule]]:
    def fetch(client: CanvasClient) -> list[CanvasModule]:
        raw_modules = client.get_list(
            f"/courses/{course_id}/modules", {"include[]": ["items", "content_details"], "per_page": 50}
        )
        modules = []
        lookups = 0
        for raw in raw_modules:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
                continue
            raw_items = raw.get("items")
            complete = True
            if not isinstance(raw_items, list):
                # Canvas leaves the items out of long modules.
                raw_items = []
                if as_int(raw.get("items_count")):
                    if lookups < MODULE_ITEM_FETCHES_MAX:
                        lookups += 1
                        raw_items = client.get_list(
                            f"/courses/{course_id}/modules/{raw['id']}/items",
                            {"include[]": "content_details", "per_page": 100},
                        )
                    else:
                        complete = False
            modules.append(parse_module(client.base_url, raw, raw_items, complete))
        modules.sort(key=lambda module: (module.position is None, module.position or 0, module.id))
        return modules

    return fetch


def fetch_assignments(course_id: int) -> Callable[[CanvasClient], list[CanvasAssignment]]:
    def fetch(client: CanvasClient) -> list[CanvasAssignment]:
        raw = client.get_list(
            f"/courses/{course_id}/assignments",
            {"include[]": "submission", "order_by": "due_at", "per_page": 100},
        )
        return [a for item in raw if (a := parse_assignment(client.base_url, item, course_id)) is not None]

    return fetch


def fetch_announcements(course_ids: list[int], today: date) -> Callable[[CanvasClient], list[CanvasAnnouncement]]:
    def fetch(client: CanvasClient) -> list[CanvasAnnouncement]:
        if not course_ids:
            return []
        raw = client.get_list(
            "/announcements",
            {
                "context_codes[]": [f"course_{course_id}" for course_id in course_ids],
                "start_date": (today - timedelta(days=ANNOUNCEMENT_DAYS)).isoformat(),
                "end_date": (today + timedelta(days=1)).isoformat(),
                "per_page": 50,
            },
        )
        announcements = [a for item in raw if (a := parse_announcement(client.base_url, item)) is not None]
        announcements.sort(key=lambda a: (a.posted_at is None, -(a.posted_at.timestamp() if a.posted_at else 0)))
        return announcements

    return fetch

