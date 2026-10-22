"""
The password policy new passwords must meet. Mirrors the frontend's
passwordValidation.ts; keep the two in sync.
"""

from fastapi import HTTPException, status

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


def check_new_password(password: str, email: str | None = None) -> None:
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Password must be at least {_MIN_LENGTH} characters.",
        )

    normalised = password.lower().replace(" ", "")
    if normalised in _BLACKLIST:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Password is too common. Please choose something more unique.",
        )

    if email:
        local_part = email.split("@")[0].lower()
        if len(local_part) >= 4 and local_part in normalised:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Password must not contain your email address.",
            )
