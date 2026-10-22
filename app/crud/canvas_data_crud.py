"""
Canvas data for the home-page panel: upcoming work, missing work, courses
with grades, a course's modules and assignments, and announcements.

What each route asks Canvas for is in canvas_fetch, and how its JSON is
normalized in canvas_normalize. Every read goes through read(), which
serves the in-process cache (core/canvas_cache.py) while it's fresh and
otherwise calls Canvas with the user's decrypted token, or fetches their
calendar feed:

- A calendar feed connection has only the "feed" kind (its items and their
  dates), and a token connection everything else; any other read raises
  CanvasNotSupported without calling Canvas.

- A connection already marked invalid, or past its token's expiry date,
  raises CanvasTokenInvalid without calling Canvas.
- A 401 from Canvas, a feed Canvas no longer knows, or a secret that no
  longer decrypts marks the connection invalid, so the panel shows
  "Reconnect Canvas", and drops the user's cached data.
- The transaction ends before Canvas is called. A fetch's outcome is only
  recorded (and cached) if the connection still holds the token it used:
  one replaced or removed meanwhile is left alone.
- If Canvas is rate-limiting after the client's backoff, or is unreachable,
  the last cached data is served with busy / stale set; with nothing
  cached, the error is raised.
- refresh=True skips a fresh cache, but not one fetched under
  MIN_REFRESH_AGE ago, so a mashed Refresh button costs one Canvas call.

Calls that reach Canvas are limited per user (_fetch_limiter), and at most
MAX_IN_FLIGHT run at once per user: Canvas charges parallel requests extra.
A fetch that can't get a slot within IN_FLIGHT_WAIT counts as Canvas
being unavailable.
The cache holds every course; hidden_course_ids is applied on the way out.
"""

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.core import canvas_cache, crypto
from app.core.canvas_client import (
    CanvasClient,
    CanvasError,
    CanvasRateLimited,
    CanvasTokenInvalid,
    CanvasUnavailable,
)
from app.core.canvas_feed import CanvasFeed, CanvasFeedInvalid
from app.core.rate_limit import RateLimiter
from app.crud.canvas_crud import decrypt_token, expired, get_connection
from app.models.canvas_connection_model import CanvasConnection

# How long each kind of data counts as fresh.
TTL = {
    "overview": timedelta(minutes=5),
    "announcements": timedelta(minutes=5),
    "courses": timedelta(minutes=15),
    "assignments": timedelta(minutes=15),
    "modules": timedelta(minutes=30),
    # The whole calendar feed, one fetch for both the overview and the linked-task sync.
    "feed": timedelta(minutes=15),
}
# The kinds a calendar feed connection can read; a token connection reads the rest.
FEED_KINDS = frozenset({"feed"})
MIN_REFRESH_AGE = timedelta(seconds=30)
MAX_IN_FLIGHT = 2
# How long a fetch waits for one of the user's MAX_IN_FLIGHT slots before serving the cache.
IN_FLIGHT_WAIT = timedelta(seconds=25)

_fetch_limiter = RateLimiter(max_calls=30, period=60)
_in_flight: defaultdict[str, threading.BoundedSemaphore] = defaultdict(
    lambda: threading.BoundedSemaphore(MAX_IN_FLIGHT)
)


class CanvasNotConnected(CanvasError):
    code = "canvas_not_connected"


class CanvasCourseHidden(CanvasError):
    code = "canvas_course_hidden"


class CanvasNotSupported(CanvasError):
    """The connection's type can't provide this data, e.g. courses from a calendar feed."""

    code = "canvas_not_supported"


@dataclass(frozen=True)
class Read:
    """Data from Canvas or the cache, and how fresh it is."""

    data: Any
    fetched_at: datetime
    # The connection's "Courses to show" setting, to filter data on the way out.
    hidden_course_ids: frozenset[int]
    busy: bool = False
    stale: bool = False


def read(
    db: Session,
    user_id: str,
    kind: str,
    key: tuple,
    fetch: Callable[[CanvasClient], Any] | Callable[[CanvasFeed], Any],
    now: datetime,
    refresh: bool = False,
    course_id: int | None = None,
    retry: bool = True,
) -> Read:
    """Serve kind/key for user_id from the cache, or fetch it from Canvas. See the module docstring.

    With course_id, a course unticked under "Courses to show" raises
    CanvasCourseHidden without calling Canvas. A token rejected after it was
    replaced or removed is read again once (with retry) against the current
    connection.
    """
    row = get_connection(db, user_id)
    if row is None:
        raise CanvasNotConnected("Canvas isn't connected.")
    if (row.connection_type == "feed") != (kind in FEED_KINDS):
        raise CanvasNotSupported(f"A {row.connection_type} connection can't read {kind}.")
    if row.status == "invalid" or expired(row, now):
        if row.connection_type == "feed":
            raise CanvasFeedInvalid("Canvas no longer knows the feed.")
        raise CanvasTokenInvalid("The token was rejected or has expired.")

    hidden = frozenset(row.hidden_course_ids or [])
    if course_id is not None and course_id in hidden:
        raise CanvasCourseHidden("The course is hidden under Courses to show.")
    cache_key = (user_id, row.base_url, row.canvas_user_id, kind, *key)
    cached = canvas_cache.get(cache_key)
    if cached is not None:
        age = now - cached.fetched_at
        if age < TTL[kind] and (not refresh or age < MIN_REFRESH_AGE):
            return Read(cached.data, cached.fetched_at, hidden)

    try:
        _fetch_limiter.check(user_id)
    except HTTPException:
        if cached is None:
            raise
        return Read(cached.data, cached.fetched_at, hidden, stale=True)

    connection_type, base_url, nonce = row.connection_type, row.base_url, row.token_nonce
    try:
        token = decrypt_token(row)
    except crypto.DecryptionError:
        token = None
    db.commit()

    in_flight = _in_flight[user_id]
    try:
        if token is None:
            raise CanvasTokenInvalid("The stored token can't be decrypted.")
        if not in_flight.acquire(timeout=IN_FLIGHT_WAIT.total_seconds()):
            raise CanvasUnavailable("Too many Canvas requests are already running for this user.")
        try:
            data = fetch(CanvasFeed(token) if connection_type == "feed" else CanvasClient(base_url, token))
        finally:
            in_flight.release()
    except CanvasTokenInvalid:
        if _record(db, user_id, nonce, {"status": "invalid", "last_error_at": now}):
            canvas_cache.evict_user(user_id)
            raise
        if not retry:
            raise
        return read(db, user_id, kind, key, fetch, now, refresh, course_id, retry=False)
    except CanvasUnavailable as exc:
        if cached is None:
            raise
        return Read(cached.data, cached.fetched_at, hidden, busy=isinstance(exc, CanvasRateLimited), stale=True)

    recorded = _record(db, user_id, nonce, {
        "last_synced_at": now,
        "status": case((CanvasConnection.status == "error", "active"), else_=CanvasConnection.status),
    })
    if not recorded:
        return Read(data, now, hidden)
    entry = canvas_cache.put(cache_key, data, now)
    return Read(entry.data, entry.fetched_at, hidden)


def _record(db: Session, user_id: str, nonce: bytes, values: dict) -> bool:
    """Write a fetch's outcome to the connection, unless it was removed or its token replaced meanwhile."""
    matched = (
        db.query(CanvasConnection)
        .filter(CanvasConnection.user_id == user_id, CanvasConnection.token_nonce == nonce)
        .update(values, synchronize_session=False)
    )
    db.commit()
    return matched > 0
