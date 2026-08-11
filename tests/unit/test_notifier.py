"""Operator alerts: what each transport actually sends, and what it swallows.

`test_outbound_and_ops.py` proves every alert code reaches *a* transport. This
proves what the shipped transport puts on the wire, which nothing else does —
the ntfy priority mapping and the Telegram body were written once and never
read back.

The rule the whole module exists for is the last two tests: **an alert that
fails to send must never take down the thing raising it.** A gas monitor that
dies because ntfy was unreachable stops watching at exactly the wrong moment.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from crypto_processing_api.alerts import notifier
from crypto_processing_api.alerts.notifier import (
    AlertCode,
    HttpTransport,
    Severity,
    notify,
    set_transport,
)
from crypto_processing_api.config import Settings

NTFY = "https://ntfy.test/cpapi-alerts"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://cpapi:cpapi@localhost:5432/cpapi",
        "environment": "development",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every httpx.post the transport makes, without a network."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def record(url: str, **kwargs: Any) -> None:
        calls.append((url, kwargs))

    monkeypatch.setattr(notifier.httpx, "post", record)
    return calls


def test_nothing_is_sent_when_nothing_is_configured(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """A deployment with no push configured is normal. The log line still
    happens, so the alert is never lost."""
    HttpTransport(make_settings()).send(Severity.WARNING, "test.code", "hello")
    assert posted == []


@pytest.mark.parametrize(
    ("severity", "priority"),
    [(Severity.INFO, "3"), (Severity.WARNING, "4"), (Severity.CRITICAL, "5")],
)
def test_ntfy_carries_the_code_as_the_title_and_maps_the_priority(
    posted: list[tuple[str, dict[str, Any]]], severity: Severity, priority: str
) -> None:
    """An operator routes on the code and triages on the priority, so both have
    to survive the trip. The body is the prose."""
    HttpTransport(make_settings(ntfy_topic_url=NTFY)).send(
        severity, AlertCode.TRON_LOW_TRX_BALANCE.value, "the wallet is nearly out of gas"
    )

    url, kwargs = posted[0]
    assert url == NTFY
    assert kwargs["headers"]["Title"] == "tron.low_trx_balance"
    assert kwargs["headers"]["Priority"] == priority
    assert kwargs["headers"]["Tags"] == severity.value
    assert kwargs["content"] == b"the wallet is nearly out of gas"


def test_telegram_needs_both_halves_of_its_configuration(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """A token with no chat id has nowhere to send, and posting anyway would
    log a delivery failure on every alert forever."""
    HttpTransport(make_settings(telegram_bot_token="123:abc")).send(
        Severity.WARNING, "test.code", "hello"
    )
    HttpTransport(make_settings(telegram_chat_id="-100123")).send(
        Severity.WARNING, "test.code", "hello"
    )
    assert posted == []


def test_telegram_sends_the_severity_and_code_above_the_message(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    HttpTransport(make_settings(telegram_bot_token="123:abc", telegram_chat_id="-100123")).send(
        Severity.CRITICAL, AlertCode.CUSTODY_INSOLVENCY_SIGNAL.value, "the books are short"
    )

    url, kwargs = posted[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert kwargs["json"]["chat_id"] == "-100123"
    assert kwargs["json"]["text"] == "[critical] custody.insolvency_signal\nthe books are short"
    # A link preview would fetch whatever an alert message happened to contain.
    assert kwargs["json"]["disable_web_page_preview"] is True


def test_both_transports_fire_when_both_are_configured(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    HttpTransport(
        make_settings(ntfy_topic_url=NTFY, telegram_bot_token="123:abc", telegram_chat_id="-100123")
    ).send(Severity.WARNING, "test.code", "hello")
    assert [url for url, _ in posted] == [
        NTFY,
        "https://api.telegram.org/bot123:abc/sendMessage",
    ]


def test_an_unreachable_transport_does_not_reach_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monitor that dies because its notifier was down stops watching at
    exactly the moment something is wrong."""

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("no route to ntfy")

    monkeypatch.setattr(notifier.httpx, "post", explode)
    HttpTransport(make_settings(ntfy_topic_url=NTFY)).send(Severity.CRITICAL, "test.code", "hello")


def test_notify_builds_the_http_transport_from_the_settings_it_is_given(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """With no transport injected, `settings` is what decides whether an alert
    goes anywhere. Without it the alert is logged and nothing else."""
    set_transport(None)
    notify(Severity.WARNING, AlertCode.OUTBOUND_DEAD_LETTER, "parked", settings=None)
    assert posted == []

    notify(
        Severity.WARNING,
        AlertCode.OUTBOUND_DEAD_LETTER,
        "parked",
        settings=make_settings(ntfy_topic_url=NTFY),
    )
    assert [url for url, _ in posted] == [NTFY]


def test_a_plain_string_code_is_accepted_alongside_the_enum(
    posted: list[tuple[str, dict[str, Any]]],
) -> None:
    """Callers pass the enum; the signature allows a string so a fork can add
    a code without editing this module."""
    set_transport(None)
    notify(
        Severity.INFO,
        "fork.custom_code",
        "something a fork cares about",
        settings=make_settings(ntfy_topic_url=NTFY),
    )
    assert posted[0][1]["headers"]["Title"] == "fork.custom_code"
