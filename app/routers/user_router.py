"""
Account-management endpoints.

POST /update-password
  Checks the user's current password with Supabase Auth, then updates it via
  the Supabase Admin API. Rejected with 403 for OAuth accounts (e.g. Google)
  and demo sandboxes — neither has a local credential to update.

DELETE /delete-account
  1. Removes all user data from the database (every model in
     account_crud.USER_OWNED_MODELS, plus the rows that cascade from them).
  2. Calls the Supabase Admin REST API to hard-delete the auth.users record so the
     e-mail address is freed and no orphaned auth rows remain.

Required .env vars
──────────────────
SUPABASE_URL              — project URL (e.g. https://xyz.supabase.co)
SUPABASE_SERVICE_ROLE_KEY — service-role key (Settings → API → service_role)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.password_policy import check_new_password
from app.core.rate_limit import RateLimiter
from app.core.supabase_admin import delete_auth_user, update_auth_user, verify_password
from app.crud.account_crud import delete_user_rows
from app.database.database import get_db
from app.schemas.account_schema import PasswordUpdateRequest

router = APIRouter()

_OAUTH_PROVIDERS = {"google", "github", "facebook", "twitter", "apple"}

# Failed current-password checks allowed per user, to stop guessing.
_password_check_limiter = RateLimiter(max_calls=5, period=15 * 60)


def _reject_demo_sandbox(user: UserInfo) -> None:
    """Demo sandboxes have no credentials to manage and are cleaned up automatically."""
    if user.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is a demo sandbox. Sign up for your own account to do this.",
        )


# ── Update password ───────────────────────────────────────────────────────────

@router.post("/update-password", status_code=204)
def update_password(
    body: PasswordUpdateRequest,
    current_user: UserInfo = Depends(get_current_user),
):
    """
    Sets a new password for the authenticated user, once their current one
    checks out.

    Returns 403 if the account uses a federated identity (Google, GitHub, etc.)
    and has never had a local password set — updating a credential that doesn't
    exist would create a shadow credential and is a security risk.

    Returns 403 if the current password is wrong, and 429 after 5 wrong ones
    in 15 minutes.

    Returns 422 if the password violates the NIST-aligned policy.
    """
    _reject_demo_sandbox(current_user)

    if current_user.provider in _OAUTH_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your account uses {current_user.provider.capitalize()} sign-in. "
                "Password changes must be made through your identity provider."
            ),
        )

    attempt = _password_check_limiter.check(current_user.id)
    try:
        correct = bool(current_user.email) and verify_password(current_user.email, body.current_password)
    except HTTPException:
        _password_check_limiter.release(current_user.id, attempt)
        raise
    if not correct:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect.",
        )
    _password_check_limiter.release(current_user.id, attempt)

    check_new_password(body.new_password, current_user.email)

    resp = update_auth_user(current_user.id, {"password": body.new_password})
    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Password update failed. Please try again.",
        )


# ── Delete account ────────────────────────────────────────────────────────────

@router.delete("/delete-account", status_code=204)
def delete_account(
    current_user: UserInfo = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _reject_demo_sandbox(current_user)

    # ── 1. Delete every row scoped to this user ──────────────────────────────
    delete_user_rows(db, current_user.id)
    db.commit()

    # ── 2. Delete the Supabase auth user ─────────────────────────────────────
    resp = delete_auth_user(current_user.id)
    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your data was deleted, but removing the login failed. Please try again.",
        )
