"""POST /update-password: the current-password check, with Supabase mocked."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import supabase_admin
from app.core.auth import UserInfo, get_current_user
from app.core.rate_limit import RateLimiter
from app.main import app
from app.routers import user_router
from tests.conftest import USER_ID

CURRENT = "my current password"
NEW = "a brand new passphrase"


def _user(provider="email", email="test@example.com"):
    return UserInfo(id=USER_ID, email=email, provider=provider, is_anonymous=False)


@pytest.fixture
def supabase(monkeypatch):
    """Fakes the two Supabase calls and records what they were given."""
    calls = {"verify": [], "update": []}

    def verify_password(email, password):
        calls["verify"].append((email, password))
        return password == CURRENT

    def update_auth_user(user_id, changes):
        calls["update"].append((user_id, changes))
        return httpx.Response(200)

    monkeypatch.setattr(user_router, "verify_password", verify_password)
    monkeypatch.setattr(user_router, "update_auth_user", update_auth_user)
    monkeypatch.setattr(user_router, "_password_check_limiter", RateLimiter(max_calls=5, period=15 * 60))
    return calls


@pytest.fixture
def as_user():
    """Signs requests in as the given user; needs no database."""
    def sign_in(user=None):
        app.dependency_overrides[get_current_user] = lambda: user or _user()
        return TestClient(app)
    yield sign_in
    app.dependency_overrides.clear()


def test_correct_current_password_updates_it(as_user, supabase):
    resp = as_user().post("/update-password", json={"current_password": CURRENT, "new_password": NEW})
    assert resp.status_code == 204
    # Checked against the token's email, not anything from the body.
    assert supabase["verify"] == [("test@example.com", CURRENT)]
    assert supabase["update"] == [(USER_ID, {"password": NEW})]


def test_email_in_body_is_ignored(as_user, supabase):
    body = {"current_password": CURRENT, "new_password": NEW, "email": "someone@else.com"}
    assert as_user().post("/update-password", json=body).status_code == 204
    assert supabase["verify"] == [("test@example.com", CURRENT)]


def test_wrong_current_password_is_403_and_changes_nothing(as_user, supabase):
    resp = as_user().post("/update-password", json={"current_password": "not it", "new_password": NEW})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Current password is incorrect."}
    assert "not it" not in resp.text and NEW not in resp.text
    assert supabase["update"] == []


@pytest.mark.parametrize("body", [{"new_password": NEW}, {"current_password": "", "new_password": NEW}])
def test_missing_current_password_is_422(as_user, supabase, body):
    assert as_user().post("/update-password", json=body).status_code == 422
    assert supabase["verify"] == [] and supabase["update"] == []


def test_wrong_passwords_are_rate_limited(as_user, supabase):
    client = as_user()
    wrong = {"current_password": "not it", "new_password": NEW}
    for _ in range(5):
        assert client.post("/update-password", json=wrong).status_code == 403
    # Even the right password is refused once the user is locked out, without asking Supabase.
    resp = client.post("/update-password", json={"current_password": CURRENT, "new_password": NEW})
    assert resp.status_code == 429
    assert len(supabase["verify"]) == 5
    assert supabase["update"] == []


def test_concurrent_wrong_passwords_cannot_outrun_the_limit(as_user, supabase, monkeypatch):
    """Checks still waiting on Supabase already count, so parallel guesses hit the limit too."""
    arrived = threading.Semaphore(0)
    proceed = threading.Event()

    def slow_verify(email, password):
        supabase["verify"].append((email, password))
        arrived.release()
        proceed.wait(timeout=10)
        return False

    monkeypatch.setattr(user_router, "verify_password", slow_verify)
    client = as_user()
    wrong = {"current_password": "not it", "new_password": NEW}
    with ThreadPoolExecutor(max_workers=5) as pool:
        in_flight = [pool.submit(client.post, "/update-password", json=wrong) for _ in range(5)]
        for _ in range(5):
            assert arrived.acquire(timeout=10)
        # Five guesses are with Supabase; a sixth is refused without asking.
        assert client.post("/update-password", json=wrong).status_code == 429
        proceed.set()
        assert [f.result().status_code for f in in_flight] == [403] * 5
    assert len(supabase["verify"]) == 5


def test_supabase_errors_do_not_count_towards_the_limit(as_user, supabase, monkeypatch):
    def down(email, password):
        raise HTTPException(status_code=502, detail="Couldn't check the password.")

    monkeypatch.setattr(user_router, "verify_password", down)
    client = as_user()
    for _ in range(6):
        assert client.post("/update-password", json={"current_password": CURRENT, "new_password": NEW}).status_code == 502


def test_successful_checks_do_not_count_towards_the_limit(as_user, supabase):
    client = as_user()
    for _ in range(6):
        resp = client.post("/update-password", json={"current_password": CURRENT, "new_password": NEW})
        assert resp.status_code == 204


def test_oauth_user_is_refused_without_a_check(as_user, supabase):
    resp = as_user(_user(provider="google")).post(
        "/update-password", json={"current_password": CURRENT, "new_password": NEW}
    )
    assert resp.status_code == 403
    assert "Google sign-in" in resp.json()["detail"]
    assert supabase["verify"] == [] and supabase["update"] == []


def test_demo_sandbox_is_refused(as_user, supabase):
    demo = UserInfo(id=USER_ID, email="", provider="anonymous", is_anonymous=True)
    resp = as_user(demo).post("/update-password", json={"current_password": CURRENT, "new_password": NEW})
    assert resp.status_code == 403
    assert supabase["verify"] == []


@pytest.mark.parametrize("new_password", ["too short", "password123456", "test-example-passphrase"])
def test_new_password_policy_still_applies(as_user, supabase, new_password):
    resp = as_user().post("/update-password", json={"current_password": CURRENT, "new_password": new_password})
    assert resp.status_code == 422
    assert supabase["update"] == []


def test_policy_is_checked_only_after_the_current_password(as_user, supabase):
    resp = as_user().post("/update-password", json={"current_password": "not it", "new_password": "short"})
    assert resp.status_code == 403


# ── verify_password against a mocked Supabase Auth ──────────────────────────

@pytest.fixture
def auth_server(monkeypatch):
    """Routes supabase_admin's httpx calls to a fake Supabase Auth."""
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    state = {"token_status": 200, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.path == "/auth/v1/token":
            if state["token_status"] == 200:
                return httpx.Response(200, json={"access_token": "session-token"})
            return httpx.Response(state["token_status"], json={"error": "invalid_grant"})
        return httpx.Response(204)

    real_client = httpx.Client
    monkeypatch.setattr(
        supabase_admin.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return state


def test_verify_password_signs_in_then_out(auth_server):
    assert supabase_admin.verify_password("test@example.com", CURRENT) is True
    token, logout = auth_server["requests"]
    assert token.url.path == "/auth/v1/token" and token.url.params["grant_type"] == "password"
    assert token.read() == b'{"email":"test@example.com","password":"my current password"}'
    assert logout.url.path == "/auth/v1/logout"
    assert logout.headers["Authorization"] == "Bearer session-token"


def test_verify_password_wrong_password(auth_server):
    auth_server["token_status"] = 400
    assert supabase_admin.verify_password("test@example.com", "not it") is False
    assert len(auth_server["requests"]) == 1


def test_verify_password_supabase_down_is_502(auth_server):
    auth_server["token_status"] = 500
    with pytest.raises(HTTPException) as exc:
        supabase_admin.verify_password("test@example.com", CURRENT)
    assert exc.value.status_code == 502
