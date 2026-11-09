"""
Google Calendar, read-only: the OAuth flow and the two lists kanso reads.

Connecting uses the authorization-code flow with offline access and the two
narrowest read-only scopes that can list calendars and their events; nothing
here can write to Google. The refresh token is the only long-lived secret. It
is stored encrypted, and neither it nor an access token is ever logged or
returned: exceptions raised here carry a stable code and no request details.

Required env vars, all three or the feature stays off
─────────────────────────────────────────────────────
GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI
(this API's /google-calendar/callback URL). CANVAS_TOKEN_KEY encrypts the token.

Error codes:
  google_unavailable       — Google is down, slow or rate-limiting
  google_access_revoked    — Google no longer accepts the grant: reconnect
  google_scopes_missing    — the user unticked a permission on Google's consent screen
  google_no_refresh_token  — Google didn't grant offline access
"""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.core import crypto
from app.schemas.limits import TEXT_MAX, TITLE_MAX

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/calendar/v3"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)

_REQUEST_TIMEOUT = 10.0
_PAGE_SIZE = 2500
_MAX_PAGES = 20
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}
# A working-location entry marks where someone works; counting it would use up the day's free time.
_NOT_EVENTS = {"workingLocation"}


class GoogleError(Exception):
    code = "google_unavailable"


class GoogleUnavailable(GoogleError):
    pass


class GoogleAccessRevoked(GoogleError):
    code = "google_access_revoked"


class GoogleNotAccessible(GoogleAccessRevoked):
    """Google refused one resource (403/404). For one calendar's events, skip that calendar."""


class GoogleSyncTokenExpired(GoogleError):
    """The sync token is too old (410): a full sync is needed."""


class GoogleScopesMissing(GoogleError):
    code = "google_scopes_missing"


class GoogleNoRefreshToken(GoogleError):
    code = "google_no_refresh_token"


@dataclass(frozen=True, repr=False)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True, repr=False)
class TokenSet:
    access_token: str
    refresh_token: str | None
    scopes: frozenset[str]


@dataclass(frozen=True)
class EventList:
    items: list[dict]
    next_sync_token: str | None


@dataclass(frozen=True)
class ImportedEvent:
    """One Google event or instance, in the fields kanso stores."""

    google_event_id: str
    recurring_event_id: str | None
    title: str
    description: str | None
    all_day: bool
    start_date: date
    end_date: date
    starts_at: datetime
    ends_at: datetime
    time_zone: str
    html_link: str | None


# ── Configuration ────────────────────────────────────────────────────────────

def oauth_config() -> OAuthConfig | None:
    """The OAuth client from the environment, or None if any value is unset."""
    values = [
        os.getenv(name, "").strip()
        for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI")
    ]
    return OAuthConfig(*values) if all(values) else None


def configured() -> bool:
    """Whether users can connect: the OAuth client is set and a token could be stored."""
    if oauth_config() is None:
        return False
    try:
        crypto.check_key()
    except crypto.EncryptionKeyError:
        return False
    return True


def authorization_url(state: str) -> str:
    config = oauth_config()
    query = urlencode({
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # Without it Google sends a refresh token only on the first consent.
        "prompt": "consent",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _send(method: str, url: str, **kwargs) -> httpx.Response:
    """Every request to Google goes through here."""
    with httpx.Client(follow_redirects=False) as client:
        return client.request(method, url, **kwargs)


def _call(method: str, url: str, deadline: float | None, **kwargs) -> httpx.Response:
    timeout = _REQUEST_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - monotonic())
        if timeout <= 0:
            raise GoogleUnavailable("Out of time for this sync.")
    try:
        return _send(method, url, timeout=timeout, **kwargs)
    except httpx.HTTPError:
        raise GoogleUnavailable("Couldn't reach Google.") from None


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise GoogleUnavailable("Google answered with something that isn't JSON.") from None
    if not isinstance(body, dict):
        raise GoogleUnavailable("Google answered with something unexpected.")
    return body


# ── Tokens ───────────────────────────────────────────────────────────────────

def _token_request(data: dict, deadline: float | None) -> TokenSet:
    config = oauth_config()
    response = _call(
        "POST",
        TOKEN_URL,
        deadline,
        data={**data, "client_id": config.client_id, "client_secret": config.client_secret},
    )
    if response.status_code in (400, 401) and _json(response).get("error") == "invalid_grant":
        raise GoogleAccessRevoked("Google no longer accepts this grant.")
    if response.status_code != 200:
        raise GoogleUnavailable(f"Google's token endpoint answered {response.status_code}.")
    body = _json(response)
    if not body.get("access_token"):
        raise GoogleUnavailable("Google's token endpoint sent no access token.")
    return TokenSet(body["access_token"], body.get("refresh_token"), frozenset(str(body.get("scope", "")).split()))


def exchange_code(code: str, deadline: float | None = None) -> TokenSet:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": oauth_config().redirect_uri}
    return _token_request(data, deadline)


def refresh_access_token(refresh_token: str, deadline: float | None = None) -> TokenSet:
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, deadline)


def revoke(token: str) -> bool:
    """Revoke a token at Google, best effort. Whether Google confirmed it."""
    try:
        return _call("POST", REVOKE_URL, None, data={"token": token}).status_code == 200
    except GoogleError:
        return False


# ── Calendar API ─────────────────────────────────────────────────────────────

class GoogleCalendarClient:
    """One sync's reads, bounded by its deadline (a time.monotonic() value)."""

    def __init__(self, access_token: str, deadline: float | None = None):
        self._access_token = access_token
        self._deadline = deadline

    def __repr__(self) -> str:
        return "GoogleCalendarClient()"

    def _get(self, url: str, params: dict) -> dict:
        response = _call(
            "GET", url, self._deadline, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if response.status_code == 200:
            return _json(response)
        if response.status_code == 401:
            raise GoogleAccessRevoked("Google rejected the access token.")
        if response.status_code == 410:
            raise GoogleSyncTokenExpired("Google expired the sync token.")
        if response.status_code == 403 and self._rate_limited(response):
            raise GoogleUnavailable("Google is rate-limiting requests.")
        if response.status_code in (403, 404):
            raise GoogleNotAccessible(f"Google answered {response.status_code}.")
        raise GoogleUnavailable(f"Google answered {response.status_code}.")

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        try:
            errors = response.json()["error"]["errors"]
            return any(error.get("reason") in _RATE_LIMIT_REASONS for error in errors)
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def _pages(self, url: str, params: dict) -> tuple[list[dict], dict]:
        """Every page's items, and the last page."""
        items: list[dict] = []
        for _ in range(_MAX_PAGES):
            page = self._get(url, params)
            items += page.get("items") or []
            token = page.get("nextPageToken")
            if not token:
                return items, page
            params = {**params, "pageToken": token}
        raise GoogleUnavailable("Too many pages of results.")

    def calendars(self) -> list[dict]:
        return self._pages(f"{API_URL}/users/me/calendarList", {"maxResults": 250, "showHidden": "true"})[0]

    def events(
        self,
        calendar_id: str,
        sync_token: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> EventList:
        """A calendar's events, repeating ones as single instances: its changes since
        sync_token (cancelled ones included), or everything between time_min and time_max."""
        params: dict = {"singleEvents": "true", "maxResults": _PAGE_SIZE}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = time_min.isoformat()
            params["timeMax"] = time_max.isoformat()
        items, last_page = self._pages(f"{API_URL}/calendars/{quote(calendar_id, safe='')}/events", params)
        return EventList(items, last_page.get("nextSyncToken"))


# ── Reading an event ─────────────────────────────────────────────────────────

def known_zone(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def parse_event(item: dict, calendar_zone: str) -> ImportedEvent | None:
    """An item from the events list, or None if it isn't an event kanso can place."""
    start, end = item.get("start") or {}, item.get("end") or {}
    if not item.get("id") or item.get("status") == "cancelled" or item.get("eventType") in _NOT_EVENTS:
        return None
    zone_name = known_zone(start.get("timeZone")) or calendar_zone
    zone = ZoneInfo(zone_name)
    try:
        if start.get("date"):
            first = date.fromisoformat(start["date"])
            # Google's end date is the day after the last one.
            last = max(first, date.fromisoformat(end["date"]) - timedelta(days=1)) if end.get("date") else first
            all_day = True
            starts_at = datetime.combine(first, time.min, tzinfo=zone).astimezone(UTC)
            ends_at = datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
        elif start.get("dateTime"):
            starts_at = _instant(start["dateTime"], zone)
            ends_at = _instant(end["dateTime"], zone) if end.get("dateTime") else starts_at
            if ends_at < starts_at:
                return None
            all_day, first, last = False, starts_at.astimezone(zone).date(), ends_at.astimezone(zone).date()
        else:
            return None
    except ValueError:
        return None
    title = str(item.get("summary") or "").strip() or "(No title)"
    description = str(item.get("description") or "").strip() or None
    return ImportedEvent(
        google_event_id=item["id"],
        recurring_event_id=item.get("recurringEventId"),
        title=title[:TITLE_MAX],
        description=description[:TEXT_MAX] if description else None,
        all_day=all_day,
        start_date=first,
        end_date=last,
        starts_at=starts_at,
        ends_at=ends_at,
        time_zone=zone_name,
        html_link=_https_link(item.get("htmlLink")),
    )


def _https_link(value) -> str | None:
    # The frontend puts it in an href, so nothing but a web link gets through.
    return value if isinstance(value, str) and value.startswith("https://") else None


def _instant(value: str, zone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).astimezone(UTC)
