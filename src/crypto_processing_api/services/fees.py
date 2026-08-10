"""Withdrawal fee estimation.

The fee is **fixed at submission time** and charged to the user, because
Greenfield payout creation has no fee-deduction field: BTCPay pays the miner
fee out of the hot wallet on top of the payout amount, so if we do not deduct
it ourselves, the float bleeds on every withdrawal.

The estimate is `feerate x assumed vsize`. The assumed vsize matters more than
it looks. A custodial deposit wallet accumulates hundreds of small UTXOs — that
is what a deposit wallet *is* — and a payout spending eight P2WPKH inputs runs
past 600 vB. Charging for 200 vB, as the original design did, is a systematic
loss that grows with volume. The default here is 300 vB and it is configurable,
with the deposit-heavy case called out in `.env.example`.

Source order: BTCPay's own wallet estimate, then mempool.space, then a static
floor. Each fallback is louder in the logs than the last, because falling all
the way through to the static rate means every withdrawal is being priced off a
guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

import httpx

from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway

logger = get_logger(__name__)

MEMPOOL_TIMEOUT = 8.0


class FeeError(Exception):
    """The fee could not be computed, or the result is not payable."""


class DustAmount(FeeError):
    """What would reach the destination is below the dust threshold."""


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """What the user will be charged, and where the number came from."""

    fee: int
    net: int
    #: Miner fee this wallet expects to pay. Equals `fee` under `deduct`; under
    #: `absorb` the user pays nothing and the operator carries this.
    wallet_fee: int
    sat_per_vb: float
    source: str

    @property
    def committed(self) -> int:
        """Custody committed to the payout: what leaves the wallet in total."""
        return self.net + self.wallet_fee


def _btcpay_rate(
    gateway: BTCPayGateway, settings: Settings, payment_method_id: str
) -> float | None:
    try:
        rate = gateway.get_fee_rate(payment_method_id, block_target=settings.btc_fee_target_blocks)
    except BTCPayError as exc:
        logger.warning("fees.btcpay_unavailable", error=str(exc))
        return None
    if rate <= 0:
        logger.warning("fees.btcpay_nonsense", rate=rate)
        return None
    return rate


def _mempool_rate(settings: Settings) -> float | None:
    if not settings.mempool_space_url:
        return None
    try:
        response = httpx.get(settings.mempool_space_url, timeout=MEMPOOL_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("fees.mempool_unavailable", error=type(exc).__name__)
        return None
    # mempool.space returns fastest/halfHour/hour/economy/minimum.
    rate = payload.get("halfHourFee") or payload.get("hourFee")
    if not isinstance(rate, int | float) or rate <= 0:
        return None
    return float(rate)


def estimate_sat_per_vb(
    gateway: BTCPayGateway, settings: Settings, *, payment_method_id: str
) -> tuple[float, str]:
    rate = _btcpay_rate(gateway, settings, payment_method_id)
    if rate is not None:
        return rate, "btcpay"

    rate = _mempool_rate(settings)
    if rate is not None:
        logger.warning("fees.using_mempool_space", rate=rate)
        return rate, "mempool.space"

    logger.error(
        "fees.using_static_fallback",
        rate=settings.btc_fallback_fee_sat_per_vb,
        detail="every withdrawal is being priced off a guess until a live source returns",
    )
    return float(settings.btc_fallback_fee_sat_per_vb), "static"


def quote_btc_fee(
    gateway: BTCPayGateway,
    settings: Settings,
    *,
    gross: int,
    payment_method_id: str,
) -> FeeQuote:
    """Price a BTC withdrawal of `gross` satoshis.

    Raises `DustAmount` when the destination would receive less than the dust
    threshold. Rejecting before the hold is placed keeps a doomed withdrawal
    from ever reserving the user's balance.
    """
    sat_per_vb, source = estimate_sat_per_vb(gateway, settings, payment_method_id=payment_method_id)
    miner_fee = int(
        (Decimal(str(sat_per_vb)) * settings.btc_payout_vsize_vb).to_integral_value(ROUND_CEILING)
    )

    if settings.withdrawal_fee_mode == "absorb":
        # The user receives the full amount and the operator pays the miner.
        quote = FeeQuote(
            fee=0, net=gross, wallet_fee=miner_fee, sat_per_vb=sat_per_vb, source=source
        )
    else:
        if miner_fee >= gross:
            raise DustAmount(
                f"the estimated network fee ({miner_fee} sat) is not smaller than "
                f"the requested amount ({gross} sat)"
            )
        quote = FeeQuote(
            fee=miner_fee,
            net=gross - miner_fee,
            wallet_fee=miner_fee,
            sat_per_vb=sat_per_vb,
            source=source,
        )

    if quote.net <= settings.btc_dust_threshold_sat:
        raise DustAmount(
            f"after fees the destination would receive {quote.net} sat, at or below the "
            f"{settings.btc_dust_threshold_sat} sat dust threshold"
        )
    return quote


def flat_fee_quote(*, gross: int, flat_fee: int, dust_threshold: int = 0) -> FeeQuote:
    """Fee for an asset with a fixed service fee — USDT in M4.

    TRC-20 transfers cost TRX energy, not USDT, so the fee is a service charge
    covering gas rather than an estimate of anything.
    """
    if flat_fee >= gross:
        raise DustAmount(f"the flat fee ({flat_fee}) is not smaller than the amount ({gross})")
    net = gross - flat_fee
    if net <= dust_threshold:
        raise DustAmount(f"after the flat fee only {net} units would arrive")
    return FeeQuote(fee=flat_fee, net=net, wallet_fee=0, sat_per_vb=0.0, source="asset_flat_fee")
