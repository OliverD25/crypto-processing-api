"""The log pipeline must not be able to emit a secret."""

from __future__ import annotations

import pytest

from crypto_processing_api.core.redaction import (
    REDACTED,
    redact_value,
    redaction_processor,
    truncate_address,
)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "APIKey",
        "btcpay_api_key",
        "secret",
        "webhook_secret",
        "BTCPAY_WEBHOOK_SECRET",
        "token",
        "access_token",
        "password",
        "admin_password",
        "signature",
        "btcpay_sig_signature",
    ],
)
def test_secret_shaped_keys_are_redacted(key: str) -> None:
    assert redact_value(key, "cpk_live_reallysecretvalue") == REDACTED


@pytest.mark.parametrize("key", ["key_id", "dedup_key", "KEY_ID"])
def test_allowlisted_handles_survive(key: str) -> None:
    assert redact_value(key, "aB3xQ9zK") == "aB3xQ9zK"


def test_nested_structures_are_walked() -> None:
    event = {
        "event": "webhook.received",
        "payload": {
            "invoice": {"id": "inv_1", "secret": "hunter2"},
            "headers": [{"name": "BTCPay-Sig", "signature": "sha256=deadbeef"}],
        },
    }
    cleaned = redaction_processor(None, "info", event)
    payload = cleaned["payload"]
    assert payload["invoice"]["secret"] == REDACTED
    assert payload["invoice"]["id"] == "inv_1"
    assert payload["headers"][0]["signature"] == REDACTED


def test_a_denied_key_redacts_its_whole_subtree() -> None:
    cleaned = redact_value("api_key", {"value": "cpk_live_x", "note": "keep"})
    assert cleaned == REDACTED


def test_addresses_are_truncated_not_dropped() -> None:
    address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
    truncated = redact_value("address", address)
    assert truncated != address
    assert truncated.startswith("bcrt1q")
    assert truncated.endswith(address[-4:])
    assert "..." in truncated


def test_short_addresses_are_left_alone() -> None:
    assert truncate_address("bc1qxy") == "bc1qxy"


def test_destination_address_is_truncated() -> None:
    value = redact_value("destination_address", "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE")
    assert "..." in value


def test_amounts_and_ids_pass_through_untouched() -> None:
    event = {"amount": 50_000_000, "deposit_id": "d-1", "status": "settled"}
    assert redaction_processor(None, "info", event) == event


def test_processor_returns_a_new_mapping() -> None:
    event = {"secret": "hunter2"}
    cleaned = redaction_processor(None, "info", event)
    assert event["secret"] == "hunter2"
    assert cleaned["secret"] == REDACTED


def test_deep_recursion_is_bounded() -> None:
    deep: dict[str, object] = {"secret": "leak"}
    for _ in range(50):
        deep = {"nested": deep}
    redaction_processor(None, "info", deep)
