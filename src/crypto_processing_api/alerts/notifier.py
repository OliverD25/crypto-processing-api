"""Operator alerts.

Three transports, all optional and all cheap: a structured log line always, an
ntfy.sh topic, and a Telegram bot. On a seven-dollar-a-month box the alerting
budget is zero, and the honest observation from the threat model is that the
MVP's real security budget is (a) how little sits in the hot wallet and (b) how
fast the operator sees an alert. This is the second half of that.

The alert list is deliberately short. An operator who gets twenty alerts a day
reads none of them, so everything here is something a human must act on.

An alert that fails to send must never take down the thing raising it. A
monitor that dies because its notifier was unreachable stops watching at
exactly the wrong moment.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

import httpx

from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger

logger = get_logger("alerts")

TIMEOUT = 5.0


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCode(StrEnum):
    """Every alert this service can raise.

    Stable, machine-readable, and greppable: an operator routes on the code,
    never on the prose.
    """

    WITHDRAWAL_PENDING_APPROVAL = "withdrawal.pending_approval"
    WITHDRAWAL_CAP_HIT = "withdrawal.cap_hit"
    #: A withdrawal settled without the rail's real network fee, because the
    #: rail could not be asked. Custody will read short by that fee with no
    #: entry naming it, which is the shape of a loss — so it is said out loud
    #: once, here, rather than discovered later as an unexplained drift.
    WITHDRAWAL_FEE_UNBOOKED = "withdrawal.fee_unbooked"
    TRON_LOW_TRX_BALANCE = "tron.low_trx_balance"
    TRON_GRID_UNREACHABLE = "tron.grid_unreachable"
    OUTBOUND_DEAD_LETTER = "outbound.dead_letter"
    RECONCILIATION_SWEEP_CAUGHT_MISS = "reconciliation.sweep_caught_miss"
    WEBHOOK_SIGNATURE_FAILURE_SPIKE = "webhook.signature_failure_spike"
    LEDGER_INVARIANT_FAILURE = "ledger.invariant_failure"
    CUSTODY_INSOLVENCY_SIGNAL = "custody.insolvency_signal"


_NTFY_PRIORITY = {Severity.INFO: "3", Severity.WARNING: "4", Severity.CRITICAL: "5"}


class Transport(Protocol):
    """Injected in tests so every alert path can be asserted without a network."""

    def send(self, severity: Severity, code: str, message: str) -> None: ...


class HttpTransport:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, severity: Severity, code: str, message: str) -> None:
        if self.settings.ntfy_topic_url:
            self._post(
                self.settings.ntfy_topic_url,
                content=message.encode("utf-8"),
                headers={
                    "Title": code,
                    "Priority": _NTFY_PRIORITY[severity],
                    "Tags": severity.value,
                },
            )
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            self._post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": f"[{severity.value}] {code}\n{message}",
                    "disable_web_page_preview": True,
                },
            )

    @staticmethod
    def _post(url: str, **kwargs: Any) -> None:
        try:
            httpx.post(url, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("alert.delivery_failed", error=type(exc).__name__)


_transport: Transport | None = None


def set_transport(transport: Transport | None) -> None:
    """Override the transport. Tests use this; nothing else should."""
    global _transport
    _transport = transport


def notify(
    severity: Severity,
    code: AlertCode | str,
    message: str,
    *,
    settings: Settings | None = None,
    **context: Any,
) -> None:
    """Raise an operator alert.

    The log line always happens, even when no transport is configured, so an
    alert is never lost just because nobody set up ntfy.
    """
    code_value = code.value if isinstance(code, AlertCode) else code
    log = {
        Severity.INFO: logger.info,
        Severity.WARNING: logger.warning,
        Severity.CRITICAL: logger.error,
    }[severity]
    log("alert", code=code_value, severity=severity.value, message=message, **context)

    transport = _transport
    if transport is None:
        if settings is None:
            return
        transport = HttpTransport(settings)
    try:
        transport.send(severity, code_value, message)
    except Exception:  # pragma: no cover - defensive
        logger.exception("alert.transport_crashed", code=code_value)
