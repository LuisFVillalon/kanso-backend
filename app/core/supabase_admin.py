"""
Supabase Admin API helpers, shared by the account-management and demo
endpoints. Every call authenticates with the service-role key, so it must
only ever act on a user id taken from a verified JWT or from the database —
never on one supplied by the client.

These are synchronous on purpose: their callers also use the (sync)
SQLAlchemy session, so they run as plain `def` handlers in FastAPI's
threadpool instead of blocking the event loop.

Required env vars
─────────────────
SUPABASE_URL              — project URL (e.g. https://xyz.supabase.co)
SUPABASE_SERVICE_ROLE_KEY — service-role key (Settings → API → service_role)
"""

import os

import httpx
from fastapi import HTTPException, status


def _config() -> tuple[str, dict[str, str]]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not service_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is not configured for account management.",
        )
    return url, {"Authorization": f"Bearer {service_key}", "apikey": service_key}


def update_auth_user(user_id: str, changes: dict) -> httpx.Response:
    url, headers = _config()
    with httpx.Client(timeout=10) as client:
        return client.put(f"{url}/auth/v1/admin/users/{user_id}", headers=headers, json=changes)


def delete_auth_user(user_id: str) -> httpx.Response:
    url, headers = _config()
    with httpx.Client(timeout=10) as client:
        return client.delete(f"{url}/auth/v1/admin/users/{user_id}", headers=headers)


def verify_password(email: str, password: str) -> bool:
    """Return whether `password` is the current password for `email`.

    Signs in with Supabase Auth's password grant, then signs that new session
    straight back out so the check leaves no session behind. The email must
    come from the verified JWT, never from the request. Raises 502 when
    Supabase can't give an answer either way.
    """
    url, headers = _config()
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{url}/auth/v1/token",
                params={"grant_type": "password"},
                headers=headers,
                json={"email": email, "password": password},
            )
            if resp.status_code == 200:
                access_token = (resp.json() or {}).get("access_token")
                if access_token:
                    try:
                        client.post(
                            f"{url}/auth/v1/logout",
                            params={"scope": "local"},
                            headers={**headers, "Authorization": f"Bearer {access_token}"},
                        )
                    except httpx.HTTPError:
                        pass  # The check already succeeded; the session just expires on its own.
                return True
    except httpx.HTTPError:
        resp = None
    # Supabase answers a wrong password with 400 invalid_grant.
    if resp is not None and resp.status_code in (400, 401, 422):
        return False
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Could not check your current password. Please try again.",
    )
