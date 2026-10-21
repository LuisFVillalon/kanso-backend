"""
AES-256-GCM encryption for third-party credentials stored at rest (the
Canvas access token).

The key never touches the database: it lives in an environment variable, a
Fly secret in production, so a leaked database dump holds only ciphertext.
Each value gets a fresh 12-byte nonce, and the owning user's id is bound in
as associated data, so a ciphertext copied into another user's row won't
decrypt.

Keys are versioned for rotation. Version 1 is CANVAS_TOKEN_KEY; version n
(n >= 2) is CANVAS_TOKEN_KEY_V{n}. New values are encrypted with the highest
version configured, and every configured version still decrypts. To rotate:
set CANVAS_TOKEN_KEY_V2, re-encrypt the stored rows, then unset the old key.

Required env vars
─────────────────
CANVAS_TOKEN_KEY — 32 random bytes, base64-encoded. Generate one with:
    python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import binascii
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "CANVAS_TOKEN_KEY"
_VERSIONED_KEY_ENV = re.compile(rf"{KEY_ENV}_V(\d+)")
_KEY_BYTES = 32
_NONCE_BYTES = 12


class EncryptionKeyError(RuntimeError):
    """The encryption key is missing or malformed."""


class DecryptionError(ValueError):
    """A ciphertext didn't decrypt: wrong key, wrong user, or tampered with."""


@dataclass(frozen=True)
class Encrypted:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def _decode_key(name: str, value: str) -> bytes:
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise EncryptionKeyError(f"{name} is not valid base64.") from None
    if len(key) != _KEY_BYTES:
        raise EncryptionKeyError(f"{name} must decode to {_KEY_BYTES} bytes, not {len(key)}.")
    return key


def _keys() -> dict[int, bytes]:
    """Every configured key, by version. Raises EncryptionKeyError if none is set or one is malformed."""
    keys: dict[int, bytes] = {}
    if os.getenv(KEY_ENV):
        keys[1] = _decode_key(KEY_ENV, os.environ[KEY_ENV])
    for name, value in os.environ.items():
        match = _VERSIONED_KEY_ENV.fullmatch(name)
        if match and value and int(match[1]) >= 2:
            keys[int(match[1])] = _decode_key(name, value)
    if not keys:
        raise EncryptionKeyError(
            f"{KEY_ENV} is not set, so Canvas tokens can't be encrypted. Generate a key with "
            "python -c \"import base64, os; print(base64.b64encode(os.urandom(32)).decode())\" "
            f"and set it with: fly secrets set {KEY_ENV}=<key>"
        )
    return keys


def check_key() -> None:
    """Raise EncryptionKeyError unless a usable key is configured."""
    _keys()


def encrypt(plaintext: str, associated_data: str) -> Encrypted:
    keys = _keys()
    version = max(keys)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(keys[version]).encrypt(nonce, plaintext.encode(), associated_data.encode())
    return Encrypted(ciphertext=ciphertext, nonce=nonce, key_version=version)


def decrypt(value: Encrypted, associated_data: str) -> str:
    key = _keys().get(value.key_version)
    if key is None:
        raise EncryptionKeyError(
            f"Key version {value.key_version} is no longer configured, so this value can't be decrypted."
        )
    try:
        return AESGCM(key).decrypt(value.nonce, value.ciphertext, associated_data.encode()).decode()
    except InvalidTag:
        raise DecryptionError("Ciphertext did not decrypt.") from None
