"""BTCPay webhook signature verification.

The failure this file exists to prevent: verifying a signature over
re-serialized JSON instead of the bytes that arrived. It passes every test
written with `json.dumps` on both sides and fails against the real BTCPay,
where it looks like an attack rather than a bug.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from crypto_processing_api.core.signing import (
    compute_btcpay_signature,
    verify_btcpay_signature,
)

SECRET = "a-webhook-secret"
BODY = b'{"deliveryId":"d1","type":"InvoiceSettled","invoiceId":"inv-1"}'


def reference_signature(secret: str, body: bytes) -> str:
    """Independent implementation of what the swagger documents."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_signature_matches_the_documented_scheme() -> None:
    assert compute_btcpay_signature(SECRET, BODY) == reference_signature(SECRET, BODY)


def test_known_vector() -> None:
    # Pinned so a refactor of the helper cannot quietly change the scheme.
    expected = hmac.new(b"secret", b"body", hashlib.sha256).hexdigest()
    assert compute_btcpay_signature("secret", b"body") == f"sha256={expected}"


def test_valid_signature_accepted() -> None:
    assert verify_btcpay_signature(SECRET, BODY, compute_btcpay_signature(SECRET, BODY))


def test_reserialized_body_does_not_verify() -> None:
    """The trap. Same JSON, different bytes, different signature."""
    signature = compute_btcpay_signature(SECRET, BODY)
    reserialized = json.dumps(json.loads(BODY)).encode("utf-8")
    assert reserialized != BODY
    assert not verify_btcpay_signature(SECRET, reserialized, signature)


def test_whitespace_difference_does_not_verify() -> None:
    signature = compute_btcpay_signature(SECRET, BODY)
    assert not verify_btcpay_signature(SECRET, BODY + b" ", signature)


def test_wrong_secret_rejected() -> None:
    assert not verify_btcpay_signature("other", BODY, compute_btcpay_signature(SECRET, BODY))


def test_empty_secret_rejected() -> None:
    """An unconfigured secret must never make everything verify."""
    assert not verify_btcpay_signature("", BODY, compute_btcpay_signature("", BODY))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "deadbeef",
        "sha1=deadbeef",
        "sha256=",
        "sha256=not-hex",
    ],
)
def test_malformed_headers_rejected(header: str | None) -> None:
    assert not verify_btcpay_signature(SECRET, BODY, header)


def test_case_difference_in_hex_rejected() -> None:
    """compare_digest is byte-exact; BTCPay sends lowercase hex."""
    signature = compute_btcpay_signature(SECRET, BODY).upper().replace("SHA256=", "sha256=")
    assert not verify_btcpay_signature(SECRET, BODY, signature)


def test_empty_body_still_signs() -> None:
    assert verify_btcpay_signature(SECRET, b"", compute_btcpay_signature(SECRET, b""))
