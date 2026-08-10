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
