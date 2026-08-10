"""Inbound API key authentication.

Key format: `cpk_live_` or `cpk_test_` followed by 32 random bytes in base62.
The first 8 characters after the prefix are the `key_id` — a non-secret lookup
handle that is safe to log and to quote in a support ticket.

The stored form is a plain SHA-256 of the full key. With 256 bits of entropy a
slow password hash buys nothing: there is no dictionary to run. Comparison is
still constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.ledger.models import ApiKey

KEY_PREFIX_LIVE = "cpk_live_"
KEY_PREFIX_TEST = "cpk_test_"
VALID_PREFIXES = (KEY_PREFIX_LIVE, KEY_PREFIX_TEST)

SECRET_BYTES = 32
#: ceil(256 / log2(62)) — the fixed width that holds 32 bytes in base62.
SECRET_CHARS = 43
KEY_ID_LENGTH = 8

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

SCOPE_READWRITE = "readwrite"
SCOPE_ADMIN = "admin"
SCOPES = (SCOPE_READWRITE, SCOPE_ADMIN)

#: An admin key can do everything a readwrite key can.
_SCOPE_RANK = {SCOPE_READWRITE: 1, SCOPE_ADMIN: 2}

#: Compared against when no record matches, so an unknown key_id costs the same
#: as a wrong secret and cannot be distinguished by response time.
_DUMMY_HASH = hashlib.sha256(b"no such key").hexdigest()

#: last_used_at is a diagnostic, not an audit trail. Writing it on every request
#: would put a row update in front of every read endpoint.
LAST_USED_RESOLUTION = timedelta(seconds=60)


class AuthError(Exception):
    """Authentication or authorization failed. The reason never reaches the client."""


class InvalidApiKey(AuthError):
    """Malformed: wrong prefix, wrong length, or characters outside base62."""


class UnknownApiKey(AuthError):
    """No record for this key_id, or the secret does not match."""


class RevokedApiKey(AuthError):
    pass


class ExpiredApiKey(AuthError):
    pass


class InsufficientScope(AuthError):
    def __init__(self, required: str, actual: str) -> None:
        super().__init__(f"scope {actual} cannot act as {required}")
        self.required = required
        self.actual = actual


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """The only moment the full key exists. It is printed once and never stored."""

    key: str
    key_id: str
    key_hash: str


@dataclass(frozen=True, slots=True)
class ParsedApiKey:
    prefix: str
    key_id: str


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    id: int
    key_id: str
    name: str
    scope: str


def _b62encode(raw: bytes, width: int) -> str:
    value = int.from_bytes(raw, "big")
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 62)
        digits.append(BASE62_ALPHABET[remainder])
    encoded = "".join(reversed(digits)) or BASE62_ALPHABET[0]
    return encoded.rjust(width, BASE62_ALPHABET[0])


def generate_api_key(prefix: str) -> GeneratedApiKey:
    if prefix not in VALID_PREFIXES:
        raise InvalidApiKey(f"unknown key prefix {prefix!r}")
    secret = _b62encode(secrets.token_bytes(SECRET_BYTES), SECRET_CHARS)
    key = f"{prefix}{secret}"
    return GeneratedApiKey(key=key, key_id=secret[:KEY_ID_LENGTH], key_hash=hash_api_key(key))


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_api_key(key: str) -> ParsedApiKey:
    for prefix in VALID_PREFIXES:
        if key.startswith(prefix):
            secret = key[len(prefix) :]
            if len(secret) != SECRET_CHARS:
                raise InvalidApiKey("key body has the wrong length")
            if any(character not in BASE62_ALPHABET for character in secret):
                raise InvalidApiKey("key body is not base62")
            return ParsedApiKey(prefix=prefix, key_id=secret[:KEY_ID_LENGTH])
    raise InvalidApiKey("key does not start with a known prefix")


def verify_api_key(presented: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(presented), stored_hash)


def has_scope(actual: str, required: str) -> bool:
    return _SCOPE_RANK.get(actual, 0) >= _SCOPE_RANK.get(required, 99)


def require_scope(authenticated: AuthenticatedKey, required: str) -> None:
    if not has_scope(authenticated.scope, required):
        raise InsufficientScope(required, authenticated.scope)


def authenticate(
    session: Session, presented: str, *, now: datetime | None = None
) -> AuthenticatedKey:
    """Resolve a bearer key to its record, or raise an `AuthError` subclass."""
    now = now or datetime.now(UTC)
    parsed = parse_api_key(presented)

    record = session.execute(
        select(ApiKey).where(ApiKey.key_id == parsed.key_id)
    ).scalar_one_or_none()
    if record is None:
        hmac.compare_digest(hash_api_key(presented), _DUMMY_HASH)
        raise UnknownApiKey("no key with that key_id")

    if not verify_api_key(presented, record.key_hash):
        raise UnknownApiKey("secret does not match")
    if record.revoked_at is not None:
        raise RevokedApiKey(f"key {record.key_id} was revoked")
    if record.expires_at is not None and record.expires_at <= now:
        raise ExpiredApiKey(f"key {record.key_id} expired")

    return AuthenticatedKey(
        id=record.id, key_id=record.key_id, name=record.name, scope=record.scope
    )


def touch_last_used(session: Session, key_id: str, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    record = session.execute(select(ApiKey).where(ApiKey.key_id == key_id)).scalar_one_or_none()
    if record is None:
        return
    if record.last_used_at is None or now - record.last_used_at >= LAST_USED_RESOLUTION:
        record.last_used_at = now


def create_api_key(
    session: Session,
    *,
    name: str,
    scope: str,
    prefix: str,
    expires_at: datetime | None = None,
) -> tuple[GeneratedApiKey, ApiKey]:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
    generated = generate_api_key(prefix)
    record = ApiKey(
        key_id=generated.key_id,
        key_hash=generated.key_hash,
        name=name,
        scope=scope,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    return generated, record


def revoke_api_key(session: Session, key_id: str, *, now: datetime | None = None) -> bool:
    """Returns False if there is no such key or it was already revoked."""
    now = now or datetime.now(UTC)
    record = session.execute(select(ApiKey).where(ApiKey.key_id == key_id)).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = now
    return True
