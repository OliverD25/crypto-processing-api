"""Operator alerts.

Deliberately small for now: a structured log line always, and an ntfy.sh POST
when one is configured. Telegram, severity routing and the full alert list are
M5.

The interface is the part that matters here — `notify(severity, code, message,
**context)` — because everything that will want to alert in M5 is being written
now, and retrofitting a call signature across a money path is worse than
shipping a thin implementation early.

An alert that fails to send must never take down the thing that was trying to
raise it. A monitor that crashes because its notifier was unreachable is a
monitor that stops watching at exactly the wrong moment.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import httpx

from crypto_processing_api.core.redaction import get_logger

logger = get_logger("alerts")

NTFY_TIMEOUT = 5.0


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


#: ntfy renders these as priorities; keep the mapping in one place.
_NTFY_PRIORITY = {Severity.INFO: "3", Severity.WARNING: "4", Severity.CRITICAL: "5"}


def notify(
    severity: Severity,
    code: str,
    message: str,
    *,
    ntfy_topic_url: str | None = None,
    **context: Any,
) -> None:
    """Raise an operator alert.

    `code` is a stable machine-readable identifier (`tron.low_trx_balance`), so
    an operator can filter or route on it without matching prose.
    """
    log = (
        logger.warning
        if severity == Severity.WARNING
        else (logger.error if severity == Severity.CRITICAL else logger.info)
    )
    log("alert", code=code, severity=severity.value, message=message, **context)

    if not ntfy_topic_url:
        return
    try:
        httpx.post(
            ntfy_topic_url,
            content=message.encode("utf-8"),
            headers={
                "Title": code,
                "Priority": _NTFY_PRIORITY[severity],
                "Tags": severity.value,
            },
            timeout=NTFY_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        # Never propagate: the caller is usually a monitor, and a monitor that
        # dies because its notifier is down stops watching.
        logger.error("alert.delivery_failed", code=code, error=type(exc).__name__)
