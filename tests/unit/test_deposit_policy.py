"""The auto-credit policy and the metadata contract, without a database.

`_target_status` is where the decision "credit this or show it to a human"
actually lives, so it gets tested on its own rather than only through the
lifecycle tests.
"""

from __future__ import annotations

import uuid

import pytest

from crypto_processing_api.gateway.btcpay_models import Invoice
from crypto_processing_api.ledger.models import DepositStatus
from crypto_processing_api.services import deposits as deposit_service
from tests.fakes import FakeBTCPay


def make_invoice(status: str, additional: str = "None") -> Invoice:
    return Invoice.model_validate(
        {
            "id": "inv-1",
            "storeId": "store-test",
            "status": status,
            "additionalStatus": additional,
            "metadata": {"cpapi": True},
        }
    )


@pytest.mark.parametrize(
    ("status", "additional", "has_payments", "expected"),
    [
        ("New", "None", False, DepositStatus.PENDING),
        ("New", "None", True, DepositStatus.CONFIRMING),
        ("Processing", "None", True, DepositStatus.CONFIRMING),
        ("Settled", "None", True, DepositStatus.SETTLED),
        ("Settled", "PaidOver", True, DepositStatus.SETTLED),
        ("Expired", "None", False, DepositStatus.EXPIRED),
    ],
)
def test_ordinary_paths(
    status: str, additional: str, has_payments: bool, expected: DepositStatus
) -> None:
    assert (
        deposit_service._target_status(make_invoice(status, additional), has_payments=has_payments)
        == expected
    )


@pytest.mark.parametrize(
    ("status", "additional", "has_payments", "why"),
    [
        ("Settled", "Marked", True, "a human clicked settled in the BTCPay UI"),
        ("Settled", "PaidLate", True, "money arrived after the window closed"),
        ("Expired", "PaidLate", True, "late payment on an expired invoice"),
        ("Expired", "PaidPartial", True, "partial payment at expiry"),
        ("Expired", "None", True, "expired but money is present"),
        ("Invalid", "Invalid", True, "BTCPay gave up on the invoice"),
        ("Invalid", "None", False, "invalid with nothing received"),
    ],
)
def test_everything_ambiguous_goes_to_a_human(
    status: str, additional: str, has_payments: bool, why: str
) -> None:
    assert (
        deposit_service._target_status(make_invoice(status, additional), has_payments=has_payments)
        == DepositStatus.REVIEW
    ), why


def test_review_is_a_dead_end_for_automation() -> None:
    assert deposit_service._ALLOWED_TARGETS[DepositStatus.REVIEW] == frozenset()


def test_dismissed_and_failed_are_frozen() -> None:
    assert deposit_service._ALLOWED_TARGETS[DepositStatus.DISMISSED] == frozenset()
    assert deposit_service._ALLOWED_TARGETS[DepositStatus.FAILED] == frozenset()


def test_settled_can_only_move_to_review() -> None:
    """A second, late payment on a settled invoice needs a human, not a credit."""
    assert deposit_service._ALLOWED_TARGETS[DepositStatus.SETTLED] == frozenset(
        {DepositStatus.REVIEW}
    )


def test_expired_can_still_settle() -> None:
    """A payment made in time but confirmed after expiry is an ordinary deposit."""
    assert DepositStatus.SETTLED in deposit_service._ALLOWED_TARGETS[DepositStatus.EXPIRED]


def test_metadata_contract() -> None:
    class Row:
        id = uuid.UUID("018f7f8c-0000-7000-8000-000000000001")
        external_user_id = "user-1"
        asset_id = "BTC"

    metadata = deposit_service.build_metadata(Row())  # type: ignore[arg-type]
    assert metadata == {
        "cpapi": True,
        "cpapi_version": 1,
        "external_user_id": "user-1",
        "deposit_id": "018f7f8c-0000-7000-8000-000000000001",
        "asset": "BTC",
    }
    assert deposit_service.is_cpapi_invoice(metadata)
    assert deposit_service.deposit_id_from_metadata(metadata) == Row.id


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"cpapi": False}, {"orderId": "someone-elses-shop"}],
)
def test_foreign_invoices_are_not_ours(metadata: dict[str, object] | None) -> None:
    assert not deposit_service.is_cpapi_invoice(metadata)


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"deposit_id": "not-a-uuid"}, {"deposit_id": 12345}],
)
def test_unusable_deposit_ids_return_none(metadata: dict[str, object] | None) -> None:
    assert deposit_service.deposit_id_from_metadata(metadata) is None


def test_fake_signs_payloads_the_endpoint_will_accept() -> None:
    from crypto_processing_api.core.signing import verify_btcpay_signature

    fake = FakeBTCPay()
    invoice = fake.create_top_up_invoice(currency="BTC", metadata={"cpapi": True})
    raw, headers = fake.sign(fake.webhook_payload("InvoiceSettled", invoice.id))
    assert verify_btcpay_signature(fake.webhook_secret, raw, headers["BTCPay-Sig"])


def test_redelivery_keeps_the_original_delivery_id() -> None:
    fake = FakeBTCPay()
    invoice = fake.create_top_up_invoice(currency="BTC", metadata={"cpapi": True})
    first = fake.webhook_payload("InvoiceSettled", invoice.id)
    retry = fake.webhook_payload(
        "InvoiceSettled", invoice.id, original_delivery_id=first["deliveryId"]
    )
    assert retry["deliveryId"] != first["deliveryId"]
    assert retry["originalDeliveryId"] == first["deliveryId"]
    assert retry["isRedelivery"] is True
