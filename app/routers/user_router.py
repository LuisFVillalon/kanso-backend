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
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.auth import UserInfo, get_current_user
from app.core.rate_limit import RateLimiter
from app.core.supabase_admin import delete_auth_user, update_auth_user, verify_password
from app.crud.account_crud import delete_user_rows
from app.database.database import get_db

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


# ── Password policy (mirrors frontend passwordValidation.ts) ─────────────────

_MIN_LENGTH = 12

# Normalised (lowercase, no spaces) common/breached passwords.
# Mirrors the BLACKLIST in passwordValidation.ts — keep the two in sync.
_BLACKLIST: frozenset[str] = frozenset({
    'password', 'password1', 'password12', 'password123', 'password1234',
    'password12345', 'password123456',
    'qwerty', 'qwerty123', 'qwertyuiop', 'qwertyuiop123',
    '123456789012', '12345678901234', '1234567890',
    'iloveyou1234', 'iloveyouforever',
    'welcome1234', 'welcome123456',
    'sunshine1234', 'princess1234',
    'monkey123456', 'dragon123456',
    'letmein12345', 'letmeinnow',
    'football1234', 'baseball1234',
    'superman1234', 'batman12345',
    'trustno11234', 'master123456',
    'admin12345678', 'administrator',
    'passphrase123', 'correcthorse',
    'mustang12345', 'shadow123456',
    'michael12345', 'jessica12345',
    'abc123456789', 'abcdefghijkl',
    'hunter12345', 'ranger12345',
    'starwars1234', 'starwars12345',
    'liverpool1234', 'chelsea12345',
    'harley12345', 'maverick1234',
})


def _check_password(password: str, email: str | None = None) -> None:
    """
    Raise HTTPException 422 if the password fails the NIST-aligned policy:
      1. Minimum 12 characters
      2. Not in the common-password blacklist
      3. Does not contain the user's email local-part (≥ 4 chars)

    Entropy scoring (zxcvbn equivalent) is enforced on the frontend only;
    the backend focuses on the objective, stateless checks that cannot be
    bypassed by the client.
    """
    if len(password) < _MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be at least {_MIN_LENGTH} characters.",
        )

    normalised = password.lower().replace(" ", "")
    if normalised in _BLACKLIST:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password is too common. Please choose something more unique.",
        )

    if email:
        local_part = email.split("@")[0].lower()
        if len(local_part) >= 4 and local_part in normalised:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Password must not contain your email address.",
            )


# ── Update password ───────────────────────────────────────────────────────────

class PasswordUpdateRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(max_length=256)

    @field_validator("new_password")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Password cannot be blank.")
        return v


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

    _password_check_limiter.ensure_room(current_user.id)
    if not current_user.email or not verify_password(current_user.email, body.current_password):
        _password_check_limiter.record(current_user.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect.",
        )

    _check_password(body.new_password, current_user.email)

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
