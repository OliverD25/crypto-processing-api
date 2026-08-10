"""HMAC signing and verification.

Inbound: BTCPay signs each webhook as `BTCPay-Sig: sha256=HMAC256(secret, rawBody)`.
Outbound (M5): the platform receives `X-CPA-Signature: t=<unix>,v1=<hmac>`.

The rule that matters for both is the same: the signature covers the exact
bytes that crossed the wire. Parsing JSON and re-serializing it changes
whitespace and key order, and the resulting signature will not match — the
failure looks like an attack rather than a bug, which is why it deserves the
comment.
"""

from __future__ import annotations

import hashlib
import hmac

BTCPAY_SIGNATURE_HEADER = "BTCPay-Sig"
_SHA256_PREFIX = "sha256="


def compute_btcpay_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"{_SHA256_PREFIX}{digest}"


def verify_btcpay_signature(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Constant-time check of a `BTCPay-Sig` header against the raw request body."""
    if not header_value or not secret:
        return False
    presented = header_value.strip()
    if not presented.startswith(_SHA256_PREFIX):
        return False
    return hmac.compare_digest(presented, compute_btcpay_signature(secret, raw_body))


PLATFORM_SIGNATURE_HEADER = "X-CPA-Signature"

#: How far apart the signed timestamp and the receiver's clock may be. Five
#: minutes is the Stripe convention and what the integration guide tells the
#: platform to enforce.
REPLAY_WINDOW_SECONDS = 300


def sign_platform_payload(secret: str, raw_body: bytes, *, timestamp: int) -> str:
    """Stripe's scheme: `t=<unix>,v1=<hex hmac-sha256("{t}.{body}")>`.

    Copying a well-documented scheme is a feature for an open-source project.
    An integrator who has ever verified a Stripe webhook already knows how to
    verify this one, including that the timestamp is inside the signed string
    so a captured request cannot be replayed a day later.
    """
    signed = f"{timestamp}.".encode() + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_platform_signature(
    secret: str,
    raw_body: bytes,
    header_value: str | None,
    *,
    now: int,
    tolerance_seconds: int = REPLAY_WINDOW_SECONDS,
) -> bool:
    """Verify our own outbound signature. Used by the tests and the docs example."""
    if not header_value or not secret:
        return False
    parts = dict(piece.split("=", 1) for piece in header_value.split(",") if "=" in piece)
    raw_timestamp, presented = parts.get("t"), parts.get("v1")
    if not raw_timestamp or not presented:
        return False
    try:
        timestamp = int(raw_timestamp)
    except ValueError:
        return False
    if abs(now - timestamp) > tolerance_seconds:
        return False
    expected = sign_platform_payload(secret, raw_body, timestamp=timestamp)
    return hmac.compare_digest(expected.split("v1=", 1)[1], presented)
