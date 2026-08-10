"""TRX gas monitor.

A TRC-20 transfer costs TRX energy and bandwidth, not USDT. So a hot wallet
holding plenty of USDT and no TRX cannot send anything, and the failure looks
like "withdrawals stopped working" rather than "top up the gas".

This is the most likely embarrassing outage in the USDT path, and it is
entirely preventable by watching one number.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_processing_api.alerts.notifier import AlertCode, Severity, notify
from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.trongrid import (
    SUN_PER_TRX,
    TronGridError,
    TronGridGateway,
    TronGridRateLimited,
)

logger = get_logger(__name__)

#: Consecutive TronGrid failures before the outage itself is worth an alert.
#: One blip is noise; a streak means verification has stopped working.
FAILURE_STREAK_ALERT = 3


@dataclass
class GasReport:
    trx_balance: int | None = None
    usdt_balance: int | None = None
    alerted: bool = False
    failures: int = 0


class GasMonitor:
    """Stateful across ticks, so it can tell a blip from an outage."""

    def __init__(self) -> None:
        self.consecutive_failures = 0

    def check(self, tron: TronGridGateway, settings: Settings) -> GasReport:
        report = GasReport()
        address = settings.tron_hot_wallet_address
        if not address:
            return report

        try:
            sun = tron.get_trx_balance(address)
            report.usdt_balance = tron.get_trc20_balance(address, settings.usdt_contract)
        except TronGridError as exc:
            self.consecutive_failures += 1
            report.failures = self.consecutive_failures
            rate_limited = isinstance(exc, TronGridRateLimited)
            if self.consecutive_failures >= FAILURE_STREAK_ALERT:
                notify(
                    Severity.WARNING,
                    AlertCode.TRON_GRID_UNREACHABLE,
                    f"TronGrid has failed {self.consecutive_failures} checks in a row"
                    + (" (rate limited)" if rate_limited else ""),
                    settings=settings,
                    error=str(exc),
                )
                report.alerted = True
            return report

        self.consecutive_failures = 0
        report.trx_balance = sun
        trx = sun // SUN_PER_TRX

        if trx < settings.trx_alert_threshold:
            notify(
                Severity.WARNING,
                AlertCode.TRON_LOW_TRX_BALANCE,
                f"TRON hot wallet has {trx} TRX, below the {settings.trx_alert_threshold} "
                "TRX threshold; USDT withdrawals will fail once it runs out of energy",
                settings=settings,
                trx=trx,
                threshold=settings.trx_alert_threshold,
            )
            report.alerted = True
        else:
            logger.info("gas_monitor.ok", trx=trx, usdt_micro=report.usdt_balance)
        return report
