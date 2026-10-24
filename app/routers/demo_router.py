"""
"Try the demo" endpoint.

Each visitor who clicks "Try the demo" gets their own private sandbox: the
frontend signs them in with a Supabase anonymous sign-in (a real, isolated
user with no email or password), then calls POST /demo/seed to fill that
sandbox with sample data. Visitors never share an account, so one can't see
or wipe another's edits.

POST /demo/seed   (authenticated, anonymous users only)
  Wipes every row the caller owns and repopulates it with sample data dated
  relative to the caller's local_date, its events in the caller's tz. Refused for any non-anonymous user,
  so it can never touch a real account's data. After responding, it deletes
  sandboxes older than DEMO_SANDBOX_TTL so anonymous users don't pile up.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.http_utils import require_time_zone
from app.core.rate_limit import RateLimiter
from app.core.supabase_admin import delete_auth_user
from app.crud.account_crud import delete_user_rows
from app.crud.demo_crud import DEFAULT_TIME_ZONE, seed_demo_data
from app.database.database import SessionLocal, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["demo"])

DEMO_SANDBOX_TTL = timedelta(days=1)
_CLEANUP_BATCH_SIZE = 25

# Seeding rewrites ~100 rows; a real visitor calls it once per trial.
_seed_limiter = RateLimiter(max_calls=5, period=60)


def _delete_stale_sandboxes() -> None:
    """Delete anonymous demo users (and all their rows) older than the TTL.

    Best-effort housekeeping: failures are logged, never surfaced to the
    visitor whose seed request triggered it.
    """
    cutoff = datetime.now(UTC) - DEMO_SANDBOX_TTL
    db = SessionLocal()
    try:
        stale_ids = db.execute(
            text(
                "SELECT id::text FROM auth.users "
                "WHERE is_anonymous AND created_at < :cutoff "
                "ORDER BY created_at LIMIT :limit"
            ),
            {"cutoff": cutoff, "limit": _CLEANUP_BATCH_SIZE},
        ).scalars().all()

        for user_id in stale_ids:
            delete_user_rows(db, user_id)
            db.commit()
            resp = delete_auth_user(user_id)
            if resp.status_code not in (200, 204, 404):
                log.warning("DEMO | could not delete sandbox user %s: %s", user_id, resp.status_code)
    except Exception:
        log.exception("DEMO | stale sandbox cleanup failed")
        db.rollback()
    finally:
        db.close()


@router.post("/seed", status_code=status.HTTP_204_NO_CONTENT)
def reset_and_seed_demo_data(
    background_tasks: BackgroundTasks,
    local_date: str | None = Query(
        None,
        description="Caller's local calendar date (YYYY-MM-DD). Seed data is dated "
        "relative to it, so 'due today' tasks and today's habit logs match the "
        "visitor's day rather than the server's.",
    ),
    tz: str | None = Query(
        None,
        description="Caller's IANA time zone, e.g. America/Los_Angeles. The sample events are at "
        "their wall times there, so classes sit inside the visitor's work windows "
        f"({DEFAULT_TIME_ZONE} if omitted).",
    ),
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reset the caller's demo sandbox to a fresh, date-relative sample set."""
    if not current_user.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo data can only be loaded into a demo sandbox.",
        )
    if tz:
        require_time_zone(tz)
    _seed_limiter.check(current_user.id)
    seed_demo_data(db, current_user.id, local_date=local_date, time_zone=tz or DEFAULT_TIME_ZONE)
    background_tasks.add_task(_delete_stale_sandboxes)
