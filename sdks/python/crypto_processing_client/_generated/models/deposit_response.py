from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.deposit_payment_response import DepositPaymentResponse


T = TypeVar("T", bound="DepositResponse")


@_attrs_define
class DepositResponse:
    """
    Attributes:
        address (None | str): What the user pays. An on-chain address, or a BOLT11 invoice for BTC_LN.
        address_reserved_until (None | str): Pooled assets only: when this address goes back to the pool.
        amount_credited (str):
        amount_expected (None | str): Display only for BTC. Load-bearing for USDT: a payment far from it goes to an
            operator instead of being credited.
        asset (str):
        checkout_link (None | str):
        created_at (str):
        deposit_id (str):
        expires_at (None | str):
        external_user_id (str):
        payments (list[DepositPaymentResponse]):
        status (str): creating, pending, confirming, settled, expired, review, dismissed, failed.
    """

    address: None | str
    address_reserved_until: None | str
    amount_credited: str
    amount_expected: None | str
    asset: str
    checkout_link: None | str
    created_at: str
    deposit_id: str
    expires_at: None | str
    external_user_id: str
    payments: list[DepositPaymentResponse]
    status: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        address: None | str
        address = self.address

        address_reserved_until: None | str
        address_reserved_until = self.address_reserved_until

        amount_credited = self.amount_credited

        amount_expected: None | str
        amount_expected = self.amount_expected

        asset = self.asset

        checkout_link: None | str
        checkout_link = self.checkout_link

        created_at = self.created_at

        deposit_id = self.deposit_id

        expires_at: None | str
        expires_at = self.expires_at

        external_user_id = self.external_user_id

        payments = []
        for payments_item_data in self.payments:
            payments_item = payments_item_data.to_dict()
            payments.append(payments_item)

        status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "address": address,
                "address_reserved_until": address_reserved_until,
                "amount_credited": amount_credited,
                "amount_expected": amount_expected,
                "asset": asset,
                "checkout_link": checkout_link,
                "created_at": created_at,
                "deposit_id": deposit_id,
                "expires_at": expires_at,
                "external_user_id": external_user_id,
                "payments": payments,
                "status": status,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deposit_payment_response import DepositPaymentResponse

        d = dict(src_dict)

        def _parse_address(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        address = _parse_address(d.pop("address"))

        def _parse_address_reserved_until(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        address_reserved_until = _parse_address_reserved_until(d.pop("address_reserved_until"))

        amount_credited = d.pop("amount_credited")

        def _parse_amount_expected(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        amount_expected = _parse_amount_expected(d.pop("amount_expected"))

        asset = d.pop("asset")

        def _parse_checkout_link(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        checkout_link = _parse_checkout_link(d.pop("checkout_link"))

        created_at = d.pop("created_at")

        deposit_id = d.pop("deposit_id")

        def _parse_expires_at(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        expires_at = _parse_expires_at(d.pop("expires_at"))

        external_user_id = d.pop("external_user_id")

        payments = []
        _payments = d.pop("payments")
        for payments_item_data in _payments:
            payments_item = DepositPaymentResponse.from_dict(payments_item_data)

            payments.append(payments_item)

        status = d.pop("status")

        deposit_response = cls(
            address=address,
            address_reserved_until=address_reserved_until,
            amount_credited=amount_credited,
            amount_expected=amount_expected,
            asset=asset,
            checkout_link=checkout_link,
            created_at=created_at,
            deposit_id=deposit_id,
            expires_at=expires_at,
            external_user_id=external_user_id,
            payments=payments,
            status=status,
        )

        deposit_response.additional_properties = d
        return deposit_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
