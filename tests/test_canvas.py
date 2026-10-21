"""Canvas connection: token encryption, the Canvas client, and the connection endpoints, with Canvas mocked."""

import asyncio
import base64
import gzip
import inspect
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import canvas_client, crypto
from app.core.auth import UserInfo, get_current_user
from app.core.canvas_client import (
    CanvasClient,
    CanvasForbidden,
    CanvasNotCanvas,
    CanvasTokenInvalid,
    CanvasUnavailable,
    CanvasUrlInvalid,
    normalize_base_url,
)
from app.core.rate_limit import RateLimiter
from app.crud.account_crud import delete_user_rows
from app.crud.canvas_crud import get_connection
from app.database.database import get_db
from app.main import app
from app.models.canvas_connection_model import CanvasConnection
from app.routers import canvas_router
from tests.conftest import USER_ID

BASE_URL = "https://sdsu.instructure.com"
HOST = "sdsu.instructure.com"
PUBLIC_IP = "151.101.1.1"
TOKEN = "7~Hq2aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3d7Hq2"
NEW_TOKEN = "7~Zz9aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3dR4nd"
OTHER_ACCOUNT_TOKEN = "7~Oo0aXbYcZdWeVfUgThSiRjQkPlOmNnMoLpKqJrIsHtGuFvEwDxCyBzA1b2c3dOthr"
EXPIRES_AT = "2026-12-15T07:59:59Z"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def canvas_key(monkeypatch):
    """A fresh CANVAS_TOKEN_KEY for every test, and no rotated keys."""
    for name in list(os.environ):
        if name.startswith(crypto.KEY_ENV):
            monkeypatch.delenv(name)
    monkeypatch.setenv(crypto.KEY_ENV, _key())


class FakeCanvas:
    """A school's Canvas API, as far as the connection endpoints use it."""

    def __init__(self):
        self.accounts = {
            TOKEN: {"id": 42, "name": "Ada Lovelace"},
            NEW_TOKEN: {"id": 42, "name": "Ada Lovelace"},
            OTHER_ACCOUNT_TOKEN: {"id": 77, "name": "Grace Hopper"},
        }
        self.token_listing = [{"id": 1, "token_hint": TOKEN[:5], "expires_at": EXPIRES_AT, "purpose": "kanso"}]
        self.requests: list[httpx.Request] = []
        # Set to a callable to answer every request differently.
        self.respond = None
        # Addresses that refuse connections.
        self.refused_addresses: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.refused_addresses:
            raise httpx.ConnectError("Connection refused")
        if self.respond:
            return self.respond(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.accounts:
            return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})
        if request.url.path == "/api/v1/users/self":
            return httpx.Response(200, json={**self.accounts[token], "sortable_name": "Lovelace, Ada"})
        if request.url.path == "/api/v1/users/self/user_generated_tokens":
            return httpx.Response(200, json=self.token_listing)
        return httpx.Response(404, json={"errors": [{"message": "The specified resource does not exist."}]})


class _NetworkBody(httpx.AsyncByteStream):
    """A body that arrives unread, like one off the network."""

    def __init__(self, content: bytes):
        self._content = content

    async def __aiter__(self):
        yield self._content


def _over_the_network(fake: FakeCanvas):
    """A handler that delivers fake's responses as a network would, with their bodies still to be read.

    httpx.Response(content=b"...") or (json=...) loads its body up front, which
    a real response never has; the client reads the raw stream to cap its size.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        response = fake(request)
        if inspect.isawaitable(response):
            response = await response
        if not response.is_stream_consumed:
            return response
        return httpx.Response(response.status_code, headers=response.headers, stream=_NetworkBody(response.content))

    return handler


@pytest.fixture
def canvas(monkeypatch):
    fake = FakeCanvas()
    monkeypatch.setattr(canvas_client, "transport", httpx.MockTransport(_over_the_network(fake)))
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: [PUBLIC_IP])
    monkeypatch.setattr(canvas_router, "_canvas_call_limiter", RateLimiter(max_calls=10, period=60))
    return fake


@pytest.fixture
def demo_client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=USER_ID, email="", provider="anonymous", is_anonymous=True
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _connect(client, token=TOKEN, base_url=BASE_URL, **body):
    return client.put("/canvas/connection", json={"base_url": base_url, "token": token, **body})


def _stored_token(row: CanvasConnection) -> str:
    return crypto.decrypt(crypto.Encrypted(row.token_ciphertext, row.token_nonce, row.key_version), row.user_id)


# ── Encryption ───────────────────────────────────────────────────────────────

def test_encrypt_round_trips_with_a_fresh_nonce_each_time():
    first = crypto.encrypt(TOKEN, USER_ID)
    second = crypto.encrypt(TOKEN, USER_ID)
    assert crypto.decrypt(first, USER_ID) == TOKEN
    assert first.key_version == 1
    assert len(first.nonce) == 12
    assert first.nonce != second.nonce and first.ciphertext != second.ciphertext
    assert TOKEN.encode() not in first.ciphertext


def test_a_ciphertext_only_decrypts_for_its_own_user():
    encrypted = crypto.encrypt(TOKEN, USER_ID)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(encrypted, "00000000-0000-0000-0000-000000000002")


def test_a_ciphertext_does_not_decrypt_under_another_key(monkeypatch):
    encrypted = crypto.encrypt(TOKEN, USER_ID)
    monkeypatch.setenv(crypto.KEY_ENV, _key())
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(encrypted, USER_ID)


def test_a_missing_key_fails_with_instructions(monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV)
    with pytest.raises(crypto.EncryptionKeyError, match="CANVAS_TOKEN_KEY is not set.*fly secrets set"):
        crypto.encrypt(TOKEN, USER_ID)


@pytest.mark.parametrize(
    ("value", "message"),
    [("not base64!", "not valid base64"), (base64.b64encode(b"short").decode(), "must decode to 32 bytes")],
)
def test_a_malformed_key_fails_clearly(monkeypatch, value, message):
    monkeypatch.setenv(crypto.KEY_ENV, value)
    with pytest.raises(crypto.EncryptionKeyError, match=message):
        crypto.check_key()


def test_new_values_use_the_newest_key_and_older_keys_still_decrypt(monkeypatch):
    old = crypto.encrypt(TOKEN, USER_ID)
    monkeypatch.setenv(f"{crypto.KEY_ENV}_V2", _key())
    new = crypto.encrypt(TOKEN, USER_ID)
    assert (old.key_version, new.key_version) == (1, 2)
    assert crypto.decrypt(old, USER_ID) == crypto.decrypt(new, USER_ID) == TOKEN

    monkeypatch.delenv(crypto.KEY_ENV)
    assert crypto.decrypt(new, USER_ID) == TOKEN
    with pytest.raises(crypto.EncryptionKeyError, match="version 1"):
        crypto.decrypt(old, USER_ID)


# ── Base URL checks ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sdsu.instructure.com", BASE_URL),
        ("  https://SDSU.Instructure.com/courses/123?module_item_id=9  ", BASE_URL),
        ("https://sdsu.instructure.com:443/", BASE_URL),
        ("https://canvas.example.edu.", "https://canvas.example.edu"),
        ("sdsu.instructure.com/login?next=https://sdsu.instructure.com/", BASE_URL),
    ],
)
def test_base_urls_are_reduced_to_an_https_host(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://sdsu.instructure.com",
        "ftp://sdsu.instructure.com",
        "https://10.0.0.5",
        "https://[::1]/",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost",
        "https://user:secret@sdsu.instructure.com",
        "https://sdsu.instructure.com:8443",
        "https://sdsu.instructure.com:notaport",
        "https://canvas..edu",
        "https://canvas_school.edu",
    ],
)
def test_unsafe_or_malformed_base_urls_are_refused(raw):
    with pytest.raises(CanvasUrlInvalid):
        normalize_base_url(raw)


@pytest.mark.parametrize("addresses", [["10.1.2.3"], ["151.101.1.1", "127.0.0.1"], ["fd00::1"], []])
def test_hosts_resolving_to_private_addresses_are_refused_before_any_request(canvas, monkeypatch, addresses):
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: addresses)
    with pytest.raises(CanvasUrlInvalid):
        CanvasClient(BASE_URL, TOKEN).get_self()
    assert canvas.requests == []


def test_hosts_that_do_not_resolve_are_refused(canvas, monkeypatch):
    def fail(host):
        raise OSError("Name or service not known")

    monkeypatch.setattr(canvas_client, "_resolve", fail)
    with pytest.raises(CanvasUrlInvalid):
        CanvasClient(BASE_URL, TOKEN).get_self()


# ── Canvas client ────────────────────────────────────────────────────────────

def test_get_self_sends_the_token_to_the_checked_address_under_the_school_host_name(canvas):
    assert CanvasClient(BASE_URL, TOKEN).get_self() == {"id": 42, "name": "Ada Lovelace"}
    [request] = canvas.requests
    assert str(request.url) == f"https://{PUBLIC_IP}/api/v1/users/self"
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


class RebindingResolver:
    """DNS that answers a public address first and a private one on every later lookup."""

    def __init__(self, private="127.0.0.1"):
        self.private = private
        self.lookups = 0

    def __call__(self, host):
        self.lookups += 1
        return [PUBLIC_IP] if self.lookups == 1 else [self.private]


@pytest.mark.parametrize("private", ["127.0.0.1", "169.254.169.254", "fdaa::3"])
def test_a_host_rebinding_to_a_private_address_after_the_check_is_never_connected_to(canvas, monkeypatch, private):
    resolver = RebindingResolver(private)
    monkeypatch.setattr(canvas_client, "_resolve", resolver)
    client = CanvasClient(BASE_URL, TOKEN)
    client.get_self()
    client.get_token_expiry()
    assert resolver.lookups == 1
    assert len(canvas.requests) == 2
    assert {r.url.host for r in canvas.requests} == {PUBLIC_IP}
    assert all(r.headers["Host"] == HOST and r.extensions["sni_hostname"] == HOST for r in canvas.requests)


def test_connecting_never_reaches_a_host_that_rebinds_to_a_private_address(client, canvas, monkeypatch):
    resolver = RebindingResolver()
    monkeypatch.setattr(canvas_client, "_resolve", resolver)
    assert _connect(client).status_code == 200
    assert resolver.lookups == 1
    assert [r.url.host for r in canvas.requests] == [PUBLIC_IP, PUBLIC_IP]


def test_a_refused_address_falls_back_to_the_next_checked_one(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: ["151.101.65.1", PUBLIC_IP])
    canvas.refused_addresses = {"151.101.65.1"}
    assert CanvasClient(BASE_URL, TOKEN).get_self()["id"] == 42
    assert [r.url.host for r in canvas.requests] == ["151.101.65.1", PUBLIC_IP]


def test_an_oversized_answer_is_abandoned_without_reading_it_all(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "MAX_RESPONSE_BYTES", 1000)
    sent = []

    async def body():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 100

    canvas.respond = lambda request: httpx.Response(200, content=body())
    with pytest.raises(CanvasNotCanvas):
        CanvasClient(BASE_URL, TOKEN).get_self()
    assert len(sent) < 100


def test_a_compressed_answer_that_would_inflate_past_the_cap_is_refused(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "MAX_RESPONSE_BYTES", 1000)
    bomb = gzip.compress(b" " * 100_000)
    assert len(bomb) < 1000
    canvas.respond = lambda request: httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})
    with pytest.raises(CanvasNotCanvas):
        CanvasClient(BASE_URL, TOKEN).get_self()
    [request] = canvas.requests
    assert request.headers["Accept-Encoding"] == "identity"


def test_a_timed_out_address_falls_back_to_the_next_checked_one(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "_resolve", lambda host: ["151.101.65.1", PUBLIC_IP])

    def respond(request):
        if request.url.host == "151.101.65.1":
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={"id": 42, "name": "Ada Lovelace"})

    canvas.respond = respond
    assert CanvasClient(BASE_URL, TOKEN).get_self()["id"] == 42
    assert [r.url.host for r in canvas.requests] == ["151.101.65.1", PUBLIC_IP]


def test_an_answer_trickling_past_the_deadline_is_abandoned(canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "DEADLINE", 0.05)

    async def body():
        for _ in range(50):
            await asyncio.sleep(0.01)
            yield b" "

    canvas.respond = lambda request: httpx.Response(200, content=body())
    with pytest.raises(CanvasUnavailable):
        CanvasClient(BASE_URL, TOKEN).get_self()


def test_a_host_stalling_before_its_headers_is_abandoned_at_the_deadline(client, canvas, monkeypatch):
    monkeypatch.setattr(canvas_client, "DEADLINE", 0.05)

    async def stall(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={"id": 42, "name": "Ada Lovelace"})

    canvas.respond = stall
    started = time.monotonic()
    response = _connect(client)
    assert response.status_code == 502
    assert response.json()["detail"] == "canvas_unavailable"
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]}), CanvasTokenInvalid),
        (httpx.Response(429, text="429 Forbidden (Rate Limit Exceeded)"), CanvasUnavailable),
        (httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)"), CanvasUnavailable),
        (httpx.Response(403, json={"status": "unauthorized"}), CanvasForbidden),
        (httpx.Response(503, text="Service Unavailable"), CanvasUnavailable),
        (httpx.Response(302, headers={"Location": "https://evil.example.com/"}), CanvasNotCanvas),
        (httpx.Response(404, text="Not Found"), CanvasNotCanvas),
        (httpx.Response(200, text="<html>Welcome to our school</html>"), CanvasNotCanvas),
        (httpx.Response(200, json={"hello": "world"}), CanvasNotCanvas),
    ],
)
def test_canvas_responses_map_to_typed_errors(canvas, response, error):
    canvas.respond = lambda request: response
    with pytest.raises(error):
        CanvasClient(BASE_URL, TOKEN).get_self()
    # Redirects are never followed.
    assert len(canvas.requests) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_network_failures_are_unavailable(canvas, exc):
    def fail(request):
        raise exc

    canvas.respond = fail
    with pytest.raises(CanvasUnavailable):
        CanvasClient(BASE_URL, TOKEN).get_self()


def test_token_expiry_is_read_from_the_matching_token_hint(canvas):
    assert CanvasClient(BASE_URL, TOKEN).get_token_expiry() == datetime(2026, 12, 15, 7, 59, 59, tzinfo=UTC)


@pytest.mark.parametrize(
    "listing",
    [
        [],
        [{"token_hint": "9~xyz", "expires_at": EXPIRES_AT}],
        [{"token_hint": TOKEN[:5], "expires_at": EXPIRES_AT}, {"token_hint": TOKEN[:4], "expires_at": EXPIRES_AT}],
        [{"token_hint": TOKEN[:5], "expires_at": None}],
        {"errors": "unexpected"},
    ],
)
def test_token_expiry_is_unknown_without_exactly_one_dated_match(canvas, listing):
    canvas.token_listing = listing
    assert CanvasClient(BASE_URL, TOKEN).get_token_expiry() is None


def test_token_expiry_is_unknown_when_the_school_refuses_the_listing(canvas):
    canvas.respond = lambda request: httpx.Response(403, json={"status": "unauthorized"})
    assert CanvasClient(BASE_URL, TOKEN).get_token_expiry() is None


# ── Connecting ───────────────────────────────────────────────────────────────

def test_get_connection_reports_not_connected(client, canvas):
    assert client.get("/canvas/connection").json()["connected"] is False


def test_connect_validates_then_stores_only_ciphertext_and_returns_a_masked_status(client, db, canvas):
    response = _connect(client, base_url="SDSU.instructure.com/courses/5")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["status"] == "active"
    assert body["base_url"] == BASE_URL
    assert body["token_last4"] == "7Hq2"
    assert body["canvas_user_id"] == 42
    assert body["canvas_user_name"] == "Ada Lovelace"
    assert datetime.fromisoformat(body["token_expires_at"]) == datetime(2026, 12, 15, 7, 59, 59, tzinfo=UTC)
    assert body["hidden_course_ids"] == []
    assert "token" not in body
    assert TOKEN not in response.text
    assert [r.url.path for r in canvas.requests] == ["/api/v1/users/self", "/api/v1/users/self/user_generated_tokens"]

    row = get_connection(db, USER_ID)
    assert TOKEN.encode() not in row.token_ciphertext
    assert _stored_token(row) == TOKEN

    shown = client.get("/canvas/connection")
    assert shown.json() == body
    assert TOKEN not in shown.text


def test_a_rejected_token_is_not_stored(client, db, canvas):
    response = _connect(client, token="7~" + "x" * 64)
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_token_invalid")
    assert get_connection(db, USER_ID) is None


@pytest.mark.parametrize(
    "token", ["short", "7~ has spaces in the middle of the token ok", "x" * 600, "7~tökenwithumlaut12345"]
)
def test_a_malformed_token_is_refused_without_calling_canvas_or_echoing_it(client, canvas, token):
    response = _connect(client, token=token)
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_token_invalid")
    assert token not in response.text
    assert canvas.requests == []


def test_a_bad_address_is_refused_without_calling_canvas(client, canvas):
    response = _connect(client, base_url="http://sdsu.instructure.com")
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_url_invalid")
    assert canvas.requests == []


def test_a_host_that_is_not_canvas_is_refused(client, db, canvas):
    canvas.respond = lambda request: httpx.Response(404, text="Not Found")
    response = _connect(client)
    assert (response.status_code, response.json()["detail"]) == (422, "canvas_not_canvas")
    assert get_connection(db, USER_ID) is None


def test_canvas_being_unreachable_is_a_bad_gateway(client, db, canvas):
    canvas.respond = lambda request: httpx.Response(502, text="Bad Gateway")
    response = _connect(client)
    assert (response.status_code, response.json()["detail"]) == (502, "canvas_unavailable")
    assert get_connection(db, USER_ID) is None


def test_connecting_without_a_key_is_refused_before_the_token_leaves_the_server(client, db, canvas, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV)
    response = _connect(client)
    assert (response.status_code, response.json()["detail"]) == (503, "canvas_not_configured")
    assert canvas.requests == []
    assert get_connection(db, USER_ID) is None


def test_replacing_with_a_new_token_for_the_same_account_keeps_settings(client, db, canvas):
    _connect(client)
    client.patch("/canvas/connection", json={"hidden_course_ids": [5]})
    response = _connect(client, token=NEW_TOKEN)
    assert response.status_code == 200
    assert response.json()["token_last4"] == "R4nd"
    assert response.json()["hidden_course_ids"] == [5]
    assert _stored_token(get_connection(db, USER_ID)) == NEW_TOKEN


def test_a_failed_replace_keeps_the_old_token(client, db, canvas):
    _connect(client)
    response = _connect(client, token="7~" + "r" * 64)
    assert response.status_code == 422
    row = get_connection(db, USER_ID)
    assert _stored_token(row) == TOKEN
    assert client.get("/canvas/connection").json()["token_last4"] == "7Hq2"


def test_replacing_with_another_account_needs_confirmation_and_resets_settings(client, db, canvas):
    _connect(client)
    client.patch("/canvas/connection", json={"hidden_course_ids": [5]})

    refused = _connect(client, token=OTHER_ACCOUNT_TOKEN)
    assert (refused.status_code, refused.json()["detail"]) == (409, "canvas_account_changed")
    assert _stored_token(get_connection(db, USER_ID)) == TOKEN

    confirmed = _connect(client, token=OTHER_ACCOUNT_TOKEN, allow_account_change=True)
    assert confirmed.status_code == 200
    assert confirmed.json()["canvas_user_name"] == "Grace Hopper"
    assert confirmed.json()["hidden_course_ids"] == []


def test_moving_to_another_school_needs_confirmation(client, canvas):
    _connect(client)
    response = _connect(client, base_url="canvas.ucsd.edu")
    assert (response.status_code, response.json()["detail"]) == (409, "canvas_account_changed")


def test_reconnecting_an_invalid_connection_makes_it_active_again(client, canvas):
    _connect(client)
    del canvas.accounts[TOKEN]
    assert client.post("/canvas/connection/test").json()["status"] == "invalid"
    body = _connect(client, token=NEW_TOKEN).json()
    assert body["status"] == "active"
    assert body["last_error_at"] is None


def test_connect_and_test_are_rate_limited(client, canvas, monkeypatch):
    monkeypatch.setattr(canvas_router, "_canvas_call_limiter", RateLimiter(max_calls=2, period=60))
    assert _connect(client).status_code == 200
    assert client.post("/canvas/connection/test").status_code == 200
    assert client.post("/canvas/connection/test").status_code == 429


# ── Testing the stored token ─────────────────────────────────────────────────

def test_test_confirms_an_active_token(client, canvas):
    _connect(client)
    canvas.accounts[TOKEN] = {"id": 42, "name": "Ada King"}
    body = client.post("/canvas/connection/test").json()
    assert body["status"] == "active"
    assert body["canvas_user_name"] == "Ada King"


def test_a_revoked_token_marks_the_connection_invalid(client, canvas):
    _connect(client)
    del canvas.accounts[TOKEN]
    response = client.post("/canvas/connection/test")
    assert response.status_code == 200
    assert response.json()["status"] == "invalid"
    assert response.json()["last_error_at"] is not None
    assert client.get("/canvas/connection").json()["status"] == "invalid"


def test_an_expired_token_is_invalid_without_calling_canvas(client, db, canvas):
    _connect(client)
    get_connection(db, USER_ID).token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()
    assert client.get("/canvas/connection").json()["status"] == "invalid"
    canvas.requests.clear()
    assert client.post("/canvas/connection/test").json()["status"] == "invalid"
    assert canvas.requests == []


def test_canvas_being_down_during_a_test_marks_an_error(client, canvas):
    _connect(client)
    canvas.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    response = client.post("/canvas/connection/test")
    assert (response.status_code, response.json()["detail"]) == (502, "canvas_unavailable")
    body = client.get("/canvas/connection").json()
    assert body["status"] == "error"
    assert body["last_error_at"] is not None


def test_a_token_that_no_longer_decrypts_needs_reconnecting(client, canvas, monkeypatch):
    _connect(client)
    monkeypatch.setenv(crypto.KEY_ENV, _key())
    canvas.requests.clear()
    assert client.post("/canvas/connection/test").json()["status"] == "invalid"
    assert canvas.requests == []


def test_testing_without_a_connection_is_not_found(client, canvas):
    response = client.post("/canvas/connection/test")
    assert (response.status_code, response.json()["detail"]) == (404, "canvas_not_connected")


# ── Settings and removal ─────────────────────────────────────────────────────

def test_patch_sets_the_hidden_courses(client, canvas):
    _connect(client)
    response = client.patch("/canvas/connection", json={"hidden_course_ids": [9, 3, 9]})
    assert response.status_code == 200
    assert response.json()["hidden_course_ids"] == [3, 9]
    assert client.get("/canvas/connection").json()["hidden_course_ids"] == [3, 9]


def test_patch_validates_course_ids(client, canvas):
    _connect(client)
    assert client.patch("/canvas/connection", json={"hidden_course_ids": [0]}).status_code == 422
    assert client.patch("/canvas/connection", json={"hidden_course_ids": list(range(1, 502))}).status_code == 422


def test_patch_without_a_connection_is_not_found(client, canvas):
    assert client.patch("/canvas/connection", json={"hidden_course_ids": []}).status_code == 404


def test_remove_hard_deletes_the_connection(client, db, canvas):
    _connect(client)
    assert client.delete("/canvas/connection").status_code == 204
    assert db.query(CanvasConnection).count() == 0
    assert client.get("/canvas/connection").json()["connected"] is False
    assert client.delete("/canvas/connection").status_code == 204


def test_account_deletion_removes_the_connection(client, db, canvas):
    _connect(client)
    delete_user_rows(db, USER_ID)
    assert db.query(CanvasConnection).count() == 0


# ── Demo sandboxes and secrecy ───────────────────────────────────────────────

@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/canvas/connection", None),
        ("PUT", "/canvas/connection", {"base_url": BASE_URL, "token": TOKEN}),
        ("POST", "/canvas/connection/test", None),
        ("PATCH", "/canvas/connection", {"hidden_course_ids": [1]}),
        ("DELETE", "/canvas/connection", None),
    ],
)
def test_demo_sandboxes_are_refused(demo_client, db, canvas, method, path, body):
    response = demo_client.request(method, path, json=body)
    assert response.status_code == 403
    assert "demo sandbox" in response.json()["detail"]
    assert canvas.requests == []
    assert get_connection(db, USER_ID) is None


def test_the_token_never_appears_in_logs(client, canvas, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(client)
    client.get("/canvas/connection")
    client.post("/canvas/connection/test")
    canvas.respond = lambda request: httpx.Response(503, text="Service Unavailable")
    client.post("/canvas/connection/test")
    _connect(client, token=NEW_TOKEN)
    assert caplog.records
    assert TOKEN not in caplog.text and NEW_TOKEN not in caplog.text
    assert TOKEN[-8:] not in caplog.text


def test_the_client_repr_hides_the_token():
    assert TOKEN not in repr(CanvasClient(BASE_URL, TOKEN))
