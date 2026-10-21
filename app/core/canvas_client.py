"""
HTTP client for a school's Canvas LMS REST API.

Every Canvas call goes through the backend with the user's personal access
token, which is decrypted per request and never sent to the browser. The
school's address comes from the user, so it is treated as untrusted input
(SSRF): it must be an https URL on a public DNS name, with no IP literals,
non-default ports or credentials, and every address the name resolves to
must be public. Redirects are never followed, so a Canvas host can't bounce
a request (and the token) somewhere else.

The token is only ever placed in the Authorization header. Nothing here logs
it, and no error message includes it; httpx's own logging records the URL
and status only.

Errors map to typed exceptions whose `code` the API returns as its detail:
  canvas_url_invalid    — the address isn't an allowed https Canvas URL
  canvas_not_canvas     — the host answered, but not like Canvas
  canvas_token_invalid  — Canvas rejected the token (401: revoked or expired)
  canvas_forbidden      — Canvas accepted the token but refused the request (403)
  canvas_unavailable    — Canvas is down, unreachable, slow or rate-limiting
"""

import ipaddress
import re
import socket
from datetime import datetime
from urllib.parse import urlsplit

import httpx

TIMEOUT = httpx.Timeout(10.0)
_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Tests swap in an httpx.MockTransport here.
transport: httpx.BaseTransport | None = None


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


def _check_public_host(host: str) -> None:
    try:
        addresses = _resolve(host)
    except OSError:
        raise CanvasUrlInvalid("That address couldn't be found.") from None
    if not addresses:
        raise CanvasUrlInvalid("That address couldn't be found.")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise CanvasUrlInvalid("That address points to a private network.")


class CanvasClient:
    """A Canvas API session for one user's token on one normalized base URL."""

    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_base_url(base_url)
        self._token = token
        self._host_checked = False

    def __repr__(self) -> str:
        return f"CanvasClient({self.base_url!r})"

    def get(self, path: str, params: dict | None = None):
        """GET /api/v1{path} and return the decoded JSON body."""
        if not self._host_checked:
            _check_public_host(urlsplit(self.base_url).hostname)
            self._host_checked = True
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
                response = client.get(
                    f"{self.base_url}/api/v1{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                )
        except httpx.TimeoutException:
            raise CanvasUnavailable("Canvas took too long to answer.") from None
        except httpx.HTTPError:
            raise CanvasUnavailable("Canvas couldn't be reached.") from None
        return _decode(response)

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


def _is_rate_limited(response: httpx.Response) -> bool:
    # Current Canvas throttles with 429; older versions sent 403 with this text.
    return response.status_code == 429 or (
        response.status_code == 403 and "rate limit exceeded" in response.text.lower()
    )


def _decode(response: httpx.Response):
    if response.status_code == 401:
        raise CanvasTokenInvalid("Canvas rejected the access token.")
    if _is_rate_limited(response):
        raise CanvasUnavailable("Canvas is rate-limiting requests.")
    if response.status_code == 403:
        raise CanvasForbidden("Canvas refused the request.")
    if response.status_code >= 500:
        raise CanvasUnavailable(f"Canvas answered {response.status_code}.")
    if response.status_code != 200:
        # Redirects (often a login page), 404s and other refusals: not a Canvas API.
        raise CanvasNotCanvas(f"The host answered {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise CanvasNotCanvas("The host didn't answer with JSON.") from None
