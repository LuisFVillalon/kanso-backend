"""
FastAPI authentication dependency using Supabase-issued JWTs.

How it works
────────────
1. The frontend (Supabase JS client) signs the user in and receives a JWT
   from Supabase Auth.  Supabase may sign tokens with HS256 (symmetric, using
   the project JWT secret) or with an asymmetric algorithm such as ES256/RS256
   (using a key-pair whose public keys are published at the JWKS endpoint).
2. Every protected API request includes the header:
       Authorization: Bearer <jwt>
3. get_current_user() inspects the token header to determine the algorithm,
   then verifies accordingly:
     • HS256 → verified with SUPABASE_JWT_SECRET
     • ES256 / RS256 (or any other alg) → verified via the Supabase JWKS
       endpoint: {SUPABASE_URL}/auth/v1/.well-known/jwks.json
4. The decoded payload's "sub" claim is the user's UUID (matches auth.users.id
   in Supabase). This UUID is used as the user_id on every user-owned row.

Environment variables required
───────────────────────────────
SUPABASE_URL         — your project URL (e.g. https://xyz.supabase.co)
SUPABASE_JWT_SECRET  — only needed if the project still signs with HS256:
                       Supabase dashboard → Project Settings → API → JWT Secret
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import jwt  # PyJWT
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def _auth_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass(frozen=True)
class UserInfo:
    """Minimal user identity extracted from the verified JWT."""
    id: str             # Supabase auth UUID (e.g. "a1b2c3d4-...")
    email: str          # from the JWT "email" claim ("" for anonymous users)
    provider: str       # primary auth provider: "email" | "google" | "anonymous" | etc.
                        # sourced from app_metadata.provider in the JWT payload
    is_anonymous: bool  # True for "Try the demo" sandbox users (Supabase anonymous sign-in)


@lru_cache(maxsize=1)
def _get_jwks_client() -> PyJWKClient:
    """Return a cached JWKS client for the Supabase project.

    URL resolution order:
      1. SUPABASE_JWKS_URL env var  — explicit override
      2. {SUPABASE_URL}/auth/v1/.well-known/jwks.json  — standard OIDC path
    """
    jwks_url = os.getenv("SUPABASE_JWKS_URL", "").strip()
    if not jwks_url:
        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        if not supabase_url:
            raise RuntimeError(
                "SUPABASE_URL is not set. "
                "Add it via: fly secrets set SUPABASE_URL=https://<project>.supabase.co"
            )
        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"

    log.info("AUTH | JWKS endpoint: %s", jwks_url)
    return PyJWKClient(jwks_url, cache_keys=True)


def _decode_token(token: str) -> dict:
    """
    Decode and verify a Supabase JWT regardless of the signing algorithm.

    • HS256  → symmetric verify with SUPABASE_JWT_SECRET
    • others → asymmetric verify via the Supabase JWKS endpoint

    All non-JWT errors are converted to jwt.InvalidTokenError so the caller
    only needs to handle one exception hierarchy.

    leeway=10 gives ±10 s of clock-skew tolerance between Supabase's auth
    server and the Fly.io VM — avoids spurious "token not yet valid" failures.
    """
    alg = jwt.get_unverified_header(token).get("alg", "")

    # Disable audience check (Supabase uses aud="authenticated" which we don't
    # need to enforce) but keep expiry and signature checks active.
    decode_options = {"verify_aud": False}
    leeway = 10  # seconds — tolerates minor clock skew

    if alg == "HS256":
        secret = os.getenv("SUPABASE_JWT_SECRET", "")
        if not secret:
            raise jwt.InvalidTokenError(
                "SUPABASE_JWT_SECRET is not configured on this server."
            )
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options=decode_options,
            leeway=leeway,
        )

    # Asymmetric algorithm (ES256, RS256, …) — fetch the matching public key.
    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        raise jwt.InvalidTokenError(
            f"Could not retrieve signing key from JWKS endpoint: {exc}"
        ) from exc
    except Exception as exc:
        raise jwt.InvalidTokenError(
            f"Unexpected JWKS error ({type(exc).__name__}): {exc}"
        ) from exc

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        options=decode_options,
        leeway=leeway,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserInfo:
    """
    FastAPI dependency — extracts and validates the Bearer JWT.

    Usage in a router:
        @router.get("/get-tasks")
        def read_tasks(current_user: UserInfo = Depends(get_current_user), ...):
            ...
    """
    if credentials is None:
        raise _auth_error("Missing Authorization header")

    token = credentials.credentials
    try:
        payload = _decode_token(token)
    except jwt.ExpiredSignatureError:
        raise _auth_error("Token has expired") from None
    except jwt.InvalidTokenError as exc:
        log.warning("AUTH | rejected — invalid token: %s", exc)
        raise _auth_error("Invalid token") from None

    user_id: str = payload.get("sub", "")
    if not user_id:
        raise _auth_error("Token payload missing 'sub' claim")

    app_meta = payload.get("app_metadata") or {}
    return UserInfo(
        id=user_id,
        email=payload.get("email") or "",
        provider=app_meta.get("provider", "email"),
        is_anonymous=bool(payload.get("is_anonymous", False)),
    )
