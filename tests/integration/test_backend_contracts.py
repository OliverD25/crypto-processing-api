"""The retrofit proof: both shipped backends pass the published contract.

`crypto_processing_api/testing/contracts.py` is what R4's new asset must pass
unmodified. A contract nothing has ever satisfied is a wish list, so the two
backends that already exist are subclassed here first — and they were written
before the contract, which is the useful direction for this evidence to run in.

The custody and fee contracts are here too, so every protocol the registry
declares has at least one implementation demonstrably conforming to it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.gateway.trongrid import TronGridError
from crypto_processing_api.ledger.models import Withdrawal, WithdrawalStatus
from crypto_processing_api.services.asset_registry import (
    BACKEND_BTCPAY_LN,
    BtcpayWalletCustody,
    DynamicChainFee,
    FlatFee,
    LightningNodeCustody,
    TronGridCustody,
)
from crypto_processing_api.services.backends import (
    BACKEND_MANUAL_TRON,
    BtcpayPayoutBackend,
    LightningPayoutBackend,
    ManualTronBackend,
    TronTxVerifier,
)
from crypto_processing_api.testing.contracts import (
    AutomatedBackendContract,
    CustodySourceContract,
    FeePolicyContract,
    OperatorBackendContract,
)
from tests.fake_tron import HOT_WALLET, USDT_CONTRACT, FakeTronGrid
from tests.fakes import FakeBTCPay, mint_bolt11, regtest_address
from tests.integration.conftest import BTC, BTC_LN, USDT

DEST = regtest_address("contract-destination")
TRON_DEST = "TN3W4H6rK2ce4vX9YnFQHwKENnHjoxb3m9"
NET = 10_000


def make_withdrawal_row(
    session: Session,
    *,
    asset: str,
    destination: str,
    backend: str,
    amount_net: int | None = None,
) -> Withdrawal:
    withdrawal = Withdrawal(
        id=uuid.uuid4(),
        external_user_id="contract-user",
        asset_id=asset,
        amount_gross=NET,
        amount_net=amount_net,
        destination_address=destination,
        status=WithdrawalStatus.APPROVED,
        approval_mode="auto",
        backend=backend,
    )
    session.add(withdrawal)
    session.commit()
    return withdrawal


# -- BtcpayPayoutBackend ---------------------------------------------------


class TestBtcpayPayoutBackendContract(AutomatedBackendContract):
    @pytest.fixture
    def backend(self, fake_btcpay: FakeBTCPay) -> BtcpayPayoutBackend:
        return BtcpayPayoutBackend(fake_btcpay, payout_method_id="BTC-CHAIN")

    @pytest.fixture
    def withdrawal(self, session: Session) -> Withdrawal:
        return make_withdrawal_row(session, asset=BTC, destination=DEST, backend="btcpay_payout")

    @pytest.fixture
    def simulate_completion(self, fake_btcpay: FakeBTCPay) -> Callable[[str], None]:
        def complete(payout_id: str) -> None:
            fake_btcpay.complete_payout(payout_id)

        return complete


# -- LightningPayoutBackend ------------------------------------------------


class TestLightningPayoutBackendContract(AutomatedBackendContract):
    """The acceptance test of the whole extension contract.

    R4's claim is that a new asset can be added through the contract without
    the contract moving. The evidence is this class: the same suite, not
    subclassed differently, not with an overridden assertion, run against a
    backend that talks to a payment rail with different money, different
    failure modes and different proof of payment.

    It passes because every method the money path uses is inherited from
    `BtcpayPayoutBackend` unchanged. What Lightning adds — the routing fee and
    the definitive-failure proof — is deliberately outside this suite, since a
    contract that demanded them would be a contract most rails could only meet
    by lying. Those two are covered in `tests/unit/test_lightning_backend.py`.
    """

    @pytest.fixture
    def backend(self, fake_btcpay: FakeBTCPay) -> LightningPayoutBackend:
        return LightningPayoutBackend(
            fake_btcpay, payout_method_id="BTC-LN", crypto_code="BTC", network="regtest"
        )

    @pytest.fixture
    def withdrawal(self, session: Session, lightning: None) -> Withdrawal:
        return make_withdrawal_row(
            session,
            asset=BTC_LN,
            destination=mint_bolt11(amount_sat=NET),
            backend=BACKEND_BTCPAY_LN,
        )

    @pytest.fixture
    def simulate_completion(self, fake_btcpay: FakeBTCPay) -> Callable[[str], None]:
        def complete(payout_id: str) -> None:
            fake_btcpay.complete_payout(payout_id)

        return complete


class TestLightningFlatFeeContract(FeePolicyContract):
    @pytest.fixture
    def policy(self) -> FlatFee:
        return FlatFee(flat_fee=100)

    @pytest.fixture
    def dust_gross(self) -> int:
        return 100


class TestLightningNodeCustodyContract(CustodySourceContract):
    @pytest.fixture
    def source(self, fake_btcpay: FakeBTCPay) -> LightningNodeCustody:
        return LightningNodeCustody(fake_btcpay, "BTC")

    @pytest.fixture
    def broken_source(self, fake_btcpay: FakeBTCPay) -> LightningNodeCustody:
        fake_btcpay.fail_next["get_lightning_balance"] = BTCPayUnavailable("the node is down")
        return LightningNodeCustody(fake_btcpay, "BTC")


# -- ManualTronBackend -----------------------------------------------------


class TestManualTronBackendContract(OperatorBackendContract):
    @pytest.fixture
    def tron(self) -> FakeTronGrid:
        return FakeTronGrid()

    @pytest.fixture
    def backend(self, tron: FakeTronGrid) -> ManualTronBackend:
        return ManualTronBackend(
            TronTxVerifier(tron, contract_address=USDT_CONTRACT, hot_wallet_address=HOT_WALLET)
        )

    @pytest.fixture
    def withdrawal(self, session: Session) -> Withdrawal:
        return make_withdrawal_row(
            session,
            asset=USDT,
            destination=TRON_DEST,
            backend=BACKEND_MANUAL_TRON,
            amount_net=NET,
        )

    @pytest.fixture
    def good_txid(self, tron: FakeTronGrid) -> str:
        return tron.add_transfer("a" * 64, recipient=TRON_DEST, amount=NET)

    @pytest.fixture
    def wrong_txids(self, tron: FakeTronGrid) -> dict[str, str]:
        """One near-miss per thing the verifier checks.

        Each of these is a real, successful, on-chain transaction. That is the
        point: "included and succeeded" is exactly the check that feels
        sufficient and is not.
        """
        return {
            "wrong recipient": tron.add_transfer(
                "b" * 64, recipient="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb", amount=NET
            ),
            "wrong amount": tron.add_transfer("c" * 64, recipient=TRON_DEST, amount=NET + 1),
            "wrong sender": tron.add_transfer(
                "d" * 64,
                recipient=TRON_DEST,
                amount=NET,
                sender="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            ),
            "wrong token contract": tron.add_transfer(
                "e" * 64,
                recipient=TRON_DEST,
                amount=NET,
                contract="TKttnV3FSY1iEoAwB4N52WK2DxdV94KpSd",
            ),
            "failed receipt": tron.add_transfer(
                "f" * 64, recipient=TRON_DEST, amount=NET, failed=True
            ),
            "no Transfer event, so nothing actually moved": tron.add_transfer(
                "1" * 64, recipient=TRON_DEST, amount=NET, include_transfer_event=False
            ),
        }


# -- fee policies ----------------------------------------------------------


class TestDynamicChainFeeContract(FeePolicyContract):
    @pytest.fixture
    def policy(self, fake_btcpay: FakeBTCPay) -> DynamicChainFee:
        return DynamicChainFee(fake_btcpay, get_settings(), "BTC-CHAIN")

    @pytest.fixture
    def dust_gross(self) -> int:
        """Below the miner fee FakeBTCPay quotes, so nothing would arrive."""
        return 100


class TestFlatFeeContract(FeePolicyContract):
    @pytest.fixture
    def policy(self) -> FlatFee:
        return FlatFee(flat_fee=1_000)

    @pytest.fixture
    def dust_gross(self) -> int:
        return 500


# -- custody sources -------------------------------------------------------


class TestBtcpayWalletCustodyContract(CustodySourceContract):
    @pytest.fixture
    def source(self, fake_btcpay: FakeBTCPay) -> BtcpayWalletCustody:
        return BtcpayWalletCustody(fake_btcpay, "BTC-CHAIN", 8)

    @pytest.fixture
    def broken_source(self, fake_btcpay: FakeBTCPay) -> BtcpayWalletCustody:
        fake_btcpay.fail_next["get_wallet"] = BTCPayUnavailable("wallet API is down")
        return BtcpayWalletCustody(fake_btcpay, "BTC-CHAIN", 8)


class TestTronGridCustodyContract(CustodySourceContract):
    @pytest.fixture
    def source(self) -> TronGridCustody:
        return TronGridCustody(FakeTronGrid(), HOT_WALLET, USDT_CONTRACT)

    @pytest.fixture
    def broken_source(self) -> TronGridCustody:
        tron = FakeTronGrid()
        tron.fail_next["get_trc20_balance"] = TronGridError("rate limited")
        return TronGridCustody(tron, HOT_WALLET, USDT_CONTRACT)
