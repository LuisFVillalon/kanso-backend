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
