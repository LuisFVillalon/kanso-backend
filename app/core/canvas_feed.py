"""
A Canvas user's private calendar feed: the read-only .ics link under
Canvas → Calendar → Calendar Feed, for schools that don't allow personal
access tokens.

The feed holds the user's assignments and calendar events with their dates,
and nothing else: no grades, submissions, announcements or course files.
Anyone holding the link can read that calendar, so the URL is a secret like
a token: it is stored encrypted, never returned in full, and never logged.
httpx logs every request's URL, so a filter on its logger blanks the feed
part of any feed URL before the record is emitted.

The link must be an https URL on a public DNS name with the path Canvas
gives every user feed, /feeds/calendars/user_<code>.ics, and no query,
port or credentials. It is fetched like the token client's requests
(core/canvas_client.get_pinned): the name is resolved once and every address
must be public, the request goes only to a checked address with TLS kept on
the host name, redirects are never followed, and the answer is capped in
size and time.

Errors:
  canvas_url_invalid       — not an allowed https address (as for a token)
  canvas_feed_url_invalid  — an https address, but not a Canvas calendar feed link
  canvas_feed_invalid      — Canvas doesn't know the feed (reset or mistyped): reconnect
  canvas_not_canvas        — the host answered, but not with a calendar
  canvas_unavailable       — Canvas is down, unreachable, slow or rate-limiting
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import httpx
from icalendar import Calendar

from app.core import canvas_client
from app.core.canvas_client import (
    CanvasNotCanvas,
    CanvasRateLimited,
    CanvasTokenInvalid,
    CanvasUnavailable,
    CanvasUrlInvalid,
    normalize_base_url,
)

# Canvas's feed codes are 40 letters and digits; a little slack either way.
FEED_PATH = re.compile(r"/feeds/calendars/user_([A-Za-z0-9]{20,128})\.ics")
# Any feed URL, wherever it appears in a log message.
_FEED_IN_TEXT = re.compile(r"/feeds/calendars/[^\s\"'?#]*")
# Canvas's UIDs name the record: event-assignment-123, event-calendar-event-456.
_UID = re.compile(r"event-([a-z_-]+)-(\d+)")
_UID_KINDS = {"assignment": "assignment", "calendar-event": "event"}
# Canvas appends the course to an item's title: "Problem Set 6 [CS 460-01]".
_SUMMARY = re.compile(r"(.*?)\s*\[([^\[\]]+)\]\s*")
_CONTEXT_COURSE = re.compile(r"(?:^|[&?,])include_contexts=(?:[^&]*,)?course_(\d+)")


class CanvasFeedUrlInvalid(CanvasUrlInvalid):
    code = "canvas_feed_url_invalid"


class CanvasFeedInvalid(CanvasTokenInvalid):
    """Canvas doesn't recognize the feed: the user reset it, or it was mistyped. Like a rejected token."""

    code = "canvas_feed_invalid"


class _RedactFeedUrls(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/feeds/calendars/" in message:
            record.msg, record.args = _FEED_IN_TEXT.sub("/feeds/calendars/[redacted]", message), ()
        return True


logging.getLogger("httpx").addFilter(_RedactFeedUrls())


@dataclass(frozen=True)
class FeedUrl:
    # The normalized feed URL: the secret.
    url: str
    # "https://host", as for a token connection.
    base_url: str
    # The feed code's last 4 characters, the only part ever shown.
    last4: str


@dataclass(frozen=True)
class FeedEvent:
    """One VEVENT, as Canvas describes it."""

    uid: str
    # "assignment", "event" or "other".
    kind: str
    # The Canvas id from the UID: the assignment's id for an assignment.
    id: int
    title: str
    course_name: str | None
    course_id: int | None
    # Aware and in UTC, or a date for an all-day item.
    start: datetime | date
    # Canvas's URL for the item, unchecked.
    url: str | None


def normalize_feed_url(raw: str) -> FeedUrl:
    """Check a pasted calendar feed link and reduce it to https://host/feeds/calendars/user_<code>.ics.

    Raises CanvasUrlInvalid for http, IP literals, ports and credentials,
    and CanvasFeedUrlInvalid for any other https address.
    """
    value = raw.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        raise CanvasUrlInvalid("Not a valid URL.") from None
    if parts.scheme.lower() != "https":
        raise CanvasUrlInvalid("The calendar feed must be an https link.")
    base_url = normalize_base_url(value)
    match = FEED_PATH.fullmatch(parts.path)
    if match is None or parts.query:
        raise CanvasFeedUrlInvalid("That isn't a Canvas calendar feed link.")
    return FeedUrl(url=f"{base_url}{parts.path}", base_url=base_url, last4=match.group(1)[-4:])


def masked(base_url: str, last4: str) -> str:
    """The feed link with all but the code's last 4 characters left out, for display."""
    return f"{base_url}/feeds/calendars/user_…{last4}.ics"


class CanvasFeed:
    """One user's calendar feed. Fetching it returns its events."""

    def __init__(self, feed_url: str):
        feed = normalize_feed_url(feed_url)
        self.base_url = feed.base_url
        self._url = feed.url
        self._host = urlsplit(feed.base_url).hostname

    def __repr__(self) -> str:
        return f"CanvasFeed({self.base_url!r})"

    def events(self) -> list[FeedEvent]:
        return parse(self.fetch())

    def fetch(self) -> bytes:
        """The feed's .ics body. Raises a CanvasError if it can't be read."""
        addresses = canvas_client._public_addresses(self._host)
        headers = {"Accept": "text/calendar", "Accept-Encoding": "identity", "Host": self._host}
        status_code, _, body = canvas_client.get_pinned(self._host, addresses, httpx.URL(self._url), headers)
        if canvas_client._is_rate_limited(status_code, body):
            raise CanvasRateLimited("Canvas is rate-limiting requests.")
        if status_code in (401, 403, 404):
            raise CanvasFeedInvalid("Canvas doesn't recognize the calendar feed.")
        if status_code >= 500:
            raise CanvasUnavailable(f"Canvas answered {status_code}.")
        if status_code != 200:
            # Redirects (often a login page) and other refusals.
            raise CanvasNotCanvas(f"The host answered {status_code}.")
        return body


def parse(body: bytes) -> list[FeedEvent]:
    """The events in an .ics body, in feed order. Raises CanvasNotCanvas if it isn't a calendar.

    Events without a Canvas UID, a title or a start are left out.
    """
    try:
        calendar = Calendar.from_ical(body)
    except Exception:
        # icalendar raises a variety of errors for text that isn't iCalendar.
        raise CanvasNotCanvas("The host didn't answer with a calendar.") from None
    if not isinstance(calendar, Calendar) or calendar.name != "VCALENDAR":
        raise CanvasNotCanvas("The host didn't answer with a calendar.")
    return [event for component in calendar.walk("VEVENT") if (event := _event(component)) is not None]


def _event(component) -> FeedEvent | None:
    uid = _text(component.get("UID"))
    summary = _text(component.get("SUMMARY"))
    start = _start(component.get("DTSTART"))
    match = _UID.fullmatch(uid or "")
    if match is None or not summary or start is None:
        return None
    title, course_name = summary, None
    bracketed = _SUMMARY.fullmatch(summary)
    if bracketed and bracketed.group(1):
        title, course_name = bracketed.group(1), bracketed.group(2).strip()
    url = _text(component.get("URL"))
    course = _CONTEXT_COURSE.search(urlsplit(url).query) if url else None
    return FeedEvent(
        uid=uid,
        kind=_UID_KINDS.get(match.group(1), "other"),
        id=int(match.group(2)),
        title=title,
        course_name=course_name,
        course_id=int(course.group(1)) if course else None,
        start=start,
        url=url,
    )


def _text(value) -> str | None:
    if value is None or isinstance(value, list):
        return None
    text = str(value).strip()
    return text or None


def _start(value) -> datetime | date | None:
    try:
        value = getattr(value, "dt", None)
    except ValueError:
        # A DTSTART icalendar couldn't read.
        return None
    if isinstance(value, datetime):
        # Canvas writes UTC; a floating time is taken as UTC too.
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return value
    return None
