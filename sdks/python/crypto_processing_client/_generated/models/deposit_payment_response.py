from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="DepositPaymentResponse")


@_attrs_define
class DepositPaymentResponse:
    """One on-chain payment against a deposit invoice.

    Attributes:
        after_expiration (bool): The payment arrived after the invoice expired, so a human decided it.
        amount (str): Decimal string in the asset's display units.
        credited (bool):
        credited_at (None | str):
        payment_id (str):
        resolved_by (None | str): `auto`, or the api-key id of the admin who resolved it.
    """

    after_expiration: bool
    amount: str
    credited: bool
    credited_at: None | str
    payment_id: str
    resolved_by: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        after_expiration = self.after_expiration

        amount = self.amount

        credited = self.credited

        credited_at: None | str
        credited_at = self.credited_at

        payment_id = self.payment_id

        resolved_by: None | str
        resolved_by = self.resolved_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "after_expiration": after_expiration,
                "amount": amount,
                "credited": credited,
                "credited_at": credited_at,
                "payment_id": payment_id,
                "resolved_by": resolved_by,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        after_expiration = d.pop("after_expiration")

        amount = d.pop("amount")

        credited = d.pop("credited")

        def _parse_credited_at(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        credited_at = _parse_credited_at(d.pop("credited_at"))

        payment_id = d.pop("payment_id")

        def _parse_resolved_by(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        resolved_by = _parse_resolved_by(d.pop("resolved_by"))

        deposit_payment_response = cls(
            after_expiration=after_expiration,
            amount=amount,
            credited=credited,
            credited_at=credited_at,
            payment_id=payment_id,
            resolved_by=resolved_by,
        )

        deposit_payment_response.additional_properties = d
        return deposit_payment_response

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
