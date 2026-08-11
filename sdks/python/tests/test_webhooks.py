"""The webhook helpers, beyond what the shared vectors already pin.

The vectors cover agreement with the server. These cover the shape of the
helper itself: what it accepts as a header, what it raises, and what it hands
back to a caller who then has to branch on the event type.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from crypto_processing_client import (
    UnknownEventTypeError,
    WebhookVerificationError,
    parse_event,
    verify_webhook,
)

SECRET = "not-a-real-secret-for-a-test"
BODY = (
    b'{"created_at":"2026-08-10T13:11:02+00:00",'
    b'"data":{"amount_credited":"0.50000000","asset":"BTC",'
    b'"deposit_id":"019feb96-7e52-771a-a8cb-a86dccc87339",'
    b'"external_user_id":"user-42","status":"settled"},'
    b'"id":"evt_019f3c1e-0a2b-7c4d-8e5f-6a7b8c9d0e1f","type":"deposit.settled"}'
)
NOW = 1760000000


def header_for(body: bytes = BODY, *, secret: str = SECRET, timestamp: int = NOW) -> str:
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_a_header_mapping_is_accepted_whatever_its_casing() -> None:
    """WSGI hands headers over in every casing there is; none of them may fail."""
    for name in ("X-CPA-Signature", "x-cpa-signature", "X-Cpa-SIGNATURE"):
        assert verify_webhook(BODY, {name: header_for()}, SECRET, now=NOW)


def test_a_missing_header_is_a_refusal_not_a_crash() -> None:
    assert not verify_webhook(BODY, {}, SECRET, now=NOW)
    assert not verify_webhook(BODY, None, SECRET, now=NOW)


def test_an_unconfigured_secret_refuses_everything() -> None:
    """The dangerous failure: an empty secret verifying whatever arrives."""
    assert not verify_webhook(BODY, header_for(secret=""), "", now=NOW)


def test_reserializing_the_body_breaks_the_signature() -> None:
    """The trap the docs lead with, asserted rather than described."""
    reserialized = json.dumps(json.loads(BODY)).encode("utf-8")
    assert reserialized != BODY
    assert not verify_webhook(reserialized, header_for(), SECRET, now=NOW)


def test_parse_event_returns_the_typed_envelope() -> None:
    event = parse_event(BODY, {"X-CPA-Signature": header_for()}, SECRET, now=NOW)
    assert event["type"] == "deposit.settled"
    assert event["data"]["amount_credited"] == "0.50000000"


def test_parse_event_refuses_an_unsigned_body() -> None:
    with pytest.raises(WebhookVerificationError):
        parse_event(BODY, {"X-CPA-Signature": "t=1,v1=deadbeef"}, SECRET, now=NOW)


def test_parse_event_refuses_a_body_that_was_changed_after_signing() -> None:
    tampered = BODY.replace(b"0.50000000", b"5.00000000")
    with pytest.raises(WebhookVerificationError):
        parse_event(tampered, header_for(), SECRET, now=NOW)


def test_an_unknown_event_type_is_its_own_error() -> None:
    """New types arrive in any minor. Skipping one must not look like a forgery."""
    body = BODY.replace(b'"deposit.settled"', b'"deposit.invented_next_year"')
    with pytest.raises(UnknownEventTypeError) as raised:
        parse_event(body, header_for(body), SECRET, now=NOW)
    assert raised.value.event_type == "deposit.invented_next_year"


def test_an_unknown_field_is_kept_rather_than_rejected() -> None:
    """Additive changes must not break a deployed integration."""
    body = BODY.replace(b'"type":"deposit.settled"', b'"invented":1,"type":"deposit.settled"')
    event = parse_event(body, header_for(body), SECRET, now=NOW)
    assert event["invented"] == 1  # type: ignore[typeddict-item]


def test_a_body_that_is_not_json_is_refused() -> None:
    body = b"<html>gateway timeout</html>"
    with pytest.raises(WebhookVerificationError):
        parse_event(body, header_for(body), SECRET, now=NOW)


def test_an_envelope_missing_a_field_is_refused() -> None:
    body = b'{"type":"deposit.settled"}'
    with pytest.raises(WebhookVerificationError):
        parse_event(body, header_for(body), SECRET, now=NOW)
