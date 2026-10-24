"""
HTTP client for a school's Canvas LMS REST API.

Every Canvas call goes through the backend with the user's personal access
token, which is decrypted per request and never sent to the browser. The
school's address comes from the user, so it is treated as untrusted input
(SSRF): it must be an https URL on a public DNS name, with no IP literals,
non-default ports or credentials, and every address the name resolves to
must be public. The name is resolved once per client, and every request
goes to an address that passed that check, with TLS still verified and SNI
and Host still naming the school's host; nothing is re-resolved in between,
so a DNS answer that changes to a private address after the check (DNS
rebinding) is never connected to. Redirects are never followed, so a Canvas
host can't bounce a request (and the token) somewhere else. A response
larger than MAX_RESPONSE_BYTES, or slower in total than DEADLINE, is
abandoned, so a hostile host can't exhaust memory or hold a worker.

Lists are paginated: get_list follows the Link header's rel="next" URL,
which Canvas makes absolute and opaque, only while it stays on the school's
host and under /api/v1, and stops after MAX_PAGES. Canvas throttles each
token with a leaky bucket, answering 429 (403 on older versions) with
"Rate Limit Exceeded" when it is empty; get_list then waits RETRY_DELAYS
(with jitter) and retries, and raises CanvasRateLimited once they run out.
It also pauses before the next page when X-Rate-Limit-Remaining runs low.

The token is only ever placed in the Authorization header. Nothing here logs
it, and no error message includes it; httpx's own logging records the URL
and status only.

Errors map to typed exceptions whose `code` the API returns as its detail:
  canvas_url_invalid    — the address isn't an allowed https Canvas URL
  canvas_not_canvas     — the host answered, but not like Canvas
  canvas_token_invalid  — Canvas rejected the token (401: revoked or expired)
  canvas_forbidden      — Canvas accepted the token but refused the request
                          (403, or a 401 saying the user isn't authorized)
  canvas_unavailable    — Canvas is down, unreachable, slow or rate-limiting
CanvasNotFound (a 404) and CanvasRateLimited are subclasses of
CanvasNotCanvas and CanvasUnavailable, keeping those codes, so callers that
care can tell them apart.
"""

import asyncio
import ipaddress
import json
import random
import re
import socket
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx

# Per connect/read; DEADLINE bounds the whole request, from connect to the last byte.
TIMEOUT = httpx.Timeout(10.0)
DEADLINE = 20.0
MAX_RESPONSE_BYTES = 5_000_000
_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_LINK = re.compile(r"<([^>]*)>([^,<]*)")
_REL_NEXT = re.compile(r";\s*rel\s*=\s*\"?([^\";]*)", re.IGNORECASE)
# Pages fetched per list; with per_page=100 that's up to 1,000 items.
MAX_PAGES = 10
# Waits, in seconds, before each retry of a rate-limited request.
RETRY_DELAYS = (1.0, 2.0, 4.0)
# Below this X-Rate-Limit-Remaining, wait LOW_QUOTA_PAUSE before the next page.
LOW_QUOTA = 100.0
LOW_QUOTA_PAUSE = 1.0

# Tests swap in an httpx.MockTransport here, and a no-op for _sleep.
transport: httpx.BaseTransport | None = None
_sleep = time.sleep


class CanvasError(Exception):
    code = "canvas_error"


class CanvasUrlInvalid(CanvasError):
    code = "canvas_url_invalid"


class CanvasNotCanvas(CanvasError):
    code = "canvas_not_canvas"


class CanvasTokenInvalid(CanvasError):
    code = "canvas_token_invalid"


class CanvasForbidden(CanvasError):
    code = "canvas_forbidden"


class CanvasUnavailable(CanvasError):
    code = "canvas_unavailable"


class CanvasNotFound(CanvasNotCanvas):
    """A 404: on the API, the course or item doesn't exist or isn't visible to the user."""


class CanvasRateLimited(CanvasUnavailable):
    """Canvas is throttling the token (429, or 403 "Rate Limit Exceeded")."""


def normalize_base_url(raw: str) -> str:
    """Reduce a pasted Canvas address to "https://host", lower-case and without a path.

    Accepts a bare host ("sdsu.instructure.com") or any page URL on it.
    Raises CanvasUrlInvalid for http, IP literals, ports, credentials and
    names that aren't fully qualified.
    """
    value = raw.strip()
    if not _SCHEME.match(value):
        value = f"https://{value}"
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise CanvasUrlInvalid("Not a valid URL.") from None
    if parts.scheme.lower() != "https":
        raise CanvasUrlInvalid("Canvas must be reached over https.")
    if parts.username is not None or parts.password is not None:
        raise CanvasUrlInvalid("The address can't include a username or password.")
    if port not in (None, 443):
        raise CanvasUrlInvalid("The address can't use a custom port.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise CanvasUrlInvalid("The address has no host name.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise CanvasUrlInvalid("Use the school's Canvas domain name, not an IP address.")
    labels = host.split(".")
    if len(labels) < 2 or not all(labels) or len(host) > 253:
        raise CanvasUrlInvalid("Use the school's full Canvas domain, e.g. school.instructure.com.")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise CanvasUrlInvalid("The host name isn't valid.") from None
    if not _HOST.fullmatch(host):
        raise CanvasUrlInvalid("The host name isn't valid.")
    return f"https://{host}"


def _resolve(host: str) -> list[str]:
    """Every address host resolves to. Tests replace this to avoid real DNS."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def _public_addresses(host: str) -> list[str]:
    """Resolve host once and return its addresses, refusing it if any is not public."""
    try:
        addresses = _resolve(host)
    except OSError:
        raise CanvasUrlInvalid("That address couldn't be found.") from None
    if not addresses:
        raise CanvasUrlInvalid("That address couldn't be found.")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise CanvasUrlInvalid("That address points to a private network.")
    return addresses


class CanvasClient:
    """A Canvas API session for one user's token on one normalized base URL."""

    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_base_url(base_url)
        self._host = urlsplit(self.base_url).hostname
        self._token = token
        # The checked addresses, resolved on the first request and reused for
        # every later one.
        self._addresses: list[str] | None = None

    def __repr__(self) -> str:
        return f"CanvasClient({self.base_url!r})"

    def get(self, path: str, params: dict | None = None):
        """GET /api/v1{path} and return the decoded JSON body."""
        return self._send(httpx.URL(f"{self.base_url}/api/v1{path}", params=params))[0]

    def get_list(self, path: str, params: dict | None = None) -> list:
        """GET a paginated list at /api/v1{path}: every page up to MAX_PAGES, retrying while rate-limited."""
        return self.get_pages(path, params)[0]

    def get_pages(self, path: str, params: dict | None = None) -> tuple[list, bool]:
        """get_list's items, and whether they are the whole list: False when MAX_PAGES cut it short."""
        url = httpx.URL(f"{self.base_url}/api/v1{path}", params=params)
        items: list = []
        for _ in range(MAX_PAGES):
            body, headers = self._send_retrying(url)
            if not isinstance(body, list):
                raise CanvasNotCanvas("The host didn't answer with a list.")
            items.extend(body)
            next_url = self._next_page(headers)
            if next_url is None:
                return items, True
            url = next_url
            if _remaining(headers) < LOW_QUOTA:
                _sleep(LOW_QUOTA_PAUSE)
        return items, False

    def _send_retrying(self, url: httpx.URL):
        for delay in RETRY_DELAYS:
            try:
                return self._send(url)
            except CanvasRateLimited:
                _sleep(delay + random.uniform(0, delay / 2))
        return self._send(url)

    def _next_page(self, headers: httpx.Headers) -> httpx.URL | None:
        """The rel="next" URL from the Link header, or None on the last page.

        Raises CanvasNotCanvas if it leaves the school's host or the API, so
        the token is never sent anywhere else.
        """
        for value in headers.get_list("Link"):
            for target, params in _LINK.findall(value):
                rels = {rel.lower() for m in _REL_NEXT.findall(params) for rel in m.split()}
                if "next" not in rels:
                    continue
                try:
                    url = httpx.URL(target.strip())
                    port = url.port
                except (httpx.InvalidURL, ValueError):
                    raise CanvasNotCanvas("The next page's address isn't valid.") from None
                if (
                    url.scheme != "https"
                    or url.host != self._host
                    or port not in (None, 443)
                    or url.userinfo
                    or not url.path.startswith("/api/v1/")
                ):
                    raise CanvasNotCanvas("The next page is outside the school's Canvas API.")
                return url
        return None

    def _send(self, url: httpx.URL):
        """GET url at a checked address; returns (decoded JSON body, response headers)."""
        if self._addresses is None:
            self._addresses = _public_addresses(self._host)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Host": self._host,
        }
        status_code, response_headers, body = get_pinned(self._host, self._addresses, url, headers)
        return _decode(status_code, response_headers, body), response_headers

    def get_self(self) -> dict:
        """The token owner's Canvas account: {"id": int, "name": str}."""
        body = self.get("/users/self")
        if not isinstance(body, dict) or not isinstance(body.get("id"), int) or not isinstance(body.get("name"), str):
            raise CanvasNotCanvas("The host didn't answer like Canvas.")
        return {"id": body["id"], "name": body["name"]}

    def get_token_expiry(self) -> datetime | None:
        """When the token expires, as Canvas reports it, or None if unknown or never.

        Matches the token against the hints on the user's generated tokens.
        Best effort: a school may not allow the listing, so any failure just
        means "unknown".
        """
        try:
            tokens = self.get("/users/self/user_generated_tokens", {"per_page": 100})
        except CanvasError:
            return None
        if not isinstance(tokens, list):
            return None
        matches = [
            t for t in tokens
            if isinstance(t, dict) and isinstance(t.get("token_hint"), str) and t["token_hint"]
            and self._token.startswith(t["token_hint"])
        ]
        if len(matches) != 1:
            return None
        try:
            return datetime.fromisoformat(matches[0]["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None


def get_pinned(host: str, addresses: list[str], url: httpx.URL, headers: dict) -> tuple[int, httpx.Headers, bytes]:
    """GET url at the first of host's checked addresses that connects; returns (status, headers, body).

    TLS (SNI and certificate verification) stays on host. Redirects aren't
    followed, and an answer over MAX_RESPONSE_BYTES or slower than DEADLINE
    is abandoned.
    """
    try:
        return asyncio.run(_get(host, addresses, url, headers))
    except (TimeoutError, httpx.TimeoutException):
        raise CanvasUnavailable("Canvas took too long to answer.") from None
    except httpx.HTTPError:
        raise CanvasUnavailable("Canvas couldn't be reached.") from None


async def _get(host: str, addresses: list[str], url: httpx.URL, headers: dict):
    # DEADLINE covers the whole exchange: connect, TLS, headers and body.
    async with asyncio.timeout(DEADLINE):
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
            for i, address in enumerate(addresses):
                try:
                    return await _fetch(client, host, url.copy_with(host=address), headers)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if i == len(addresses) - 1:
                        raise


async def _fetch(client: httpx.AsyncClient, host: str, url: httpx.URL, headers: dict):
    # The URL names the checked address; sni_hostname keeps TLS (SNI and
    # certificate verification) on the school's host name.
    async with client.stream("GET", url, headers=headers, extensions={"sni_hostname": host}) as response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CanvasNotCanvas("The host answered with an encoding that was not asked for.")
        body = bytearray()
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise CanvasNotCanvas("The host's answer was too large.")
        return response.status_code, response.headers, bytes(body)


def _is_rate_limited(status_code: int, body: bytes) -> bool:
    # Current Canvas throttles with 429; older versions sent 403 with this text.
    return status_code == 429 or (status_code == 403 and b"rate limit exceeded" in body.lower())


def _is_permission_refusal(headers: httpx.Headers, body: bytes) -> bool:
    """A 401 for a valid token that may not see this resource, e.g. a course's hidden Modules tab.

    Canvas sends WWW-Authenticate only when the token itself is bad, and
    answers {"status": "unauthorized"} when the user just lacks access.
    Anything else counts as a bad token.
    """
    if "WWW-Authenticate" in headers:
        return False
    try:
        decoded = json.loads(body)
    except ValueError:
        return False
    return isinstance(decoded, dict) and decoded.get("status") == "unauthorized"


def _remaining(headers: httpx.Headers) -> float:
    try:
        return float(headers.get("X-Rate-Limit-Remaining", "inf"))
    except ValueError:
        return float("inf")


def _decode(status_code: int, headers: httpx.Headers, body: bytes):
    if status_code == 401:
        if _is_permission_refusal(headers, body):
            raise CanvasForbidden("Canvas refused the request.")
        raise CanvasTokenInvalid("Canvas rejected the access token.")
    if _is_rate_limited(status_code, body):
        raise CanvasRateLimited("Canvas is rate-limiting requests.")
    if status_code == 403:
        raise CanvasForbidden("Canvas refused the request.")
    if status_code >= 500:
        raise CanvasUnavailable(f"Canvas answered {status_code}.")
    if status_code == 404:
        raise CanvasNotFound("The host answered 404.")
    if status_code != 200:
        # Redirects (often a login page) and other refusals: not a Canvas API.
        raise CanvasNotCanvas(f"The host answered {status_code}.")
    try:
        return json.loads(body)
    except ValueError:
        raise CanvasNotCanvas("The host didn't answer with JSON.") from None
