from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="WithdrawalResponse")


@_attrs_define
class WithdrawalResponse:
    """
    Attributes:
        amount_gross (str):
        amount_net (None | str): `null` until submission.
        approval_mode (None | str): `auto` or `manual`.
        asset (str):
        created_at (str):
        destination_address (str):
        external_user_id (str):
        failure_reason (None | str):
        fee (None | str): `null` until submission, when the fee is fixed.
        status (str): requested, pending_approval, approved, rejected, submitting, submitted, broadcast, confirmed,
            failed, refunded.
        txid (None | str):
        updated_at (str):
        withdrawal_id (str):
    """

    amount_gross: str
    amount_net: None | str
    approval_mode: None | str
    asset: str
    created_at: str
    destination_address: str
    external_user_id: str
    failure_reason: None | str
    fee: None | str
    status: str
    txid: None | str
    updated_at: str
    withdrawal_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        amount_gross = self.amount_gross

        amount_net: None | str
        amount_net = self.amount_net

        approval_mode: None | str
        approval_mode = self.approval_mode

        asset = self.asset

        created_at = self.created_at

        destination_address = self.destination_address

        external_user_id = self.external_user_id

        failure_reason: None | str
        failure_reason = self.failure_reason

        fee: None | str
        fee = self.fee

        status = self.status

        txid: None | str
        txid = self.txid

        updated_at = self.updated_at

        withdrawal_id = self.withdrawal_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "amount_gross": amount_gross,
                "amount_net": amount_net,
                "approval_mode": approval_mode,
                "asset": asset,
                "created_at": created_at,
                "destination_address": destination_address,
                "external_user_id": external_user_id,
                "failure_reason": failure_reason,
                "fee": fee,
                "status": status,
                "txid": txid,
                "updated_at": updated_at,
                "withdrawal_id": withdrawal_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        amount_gross = d.pop("amount_gross")

        def _parse_amount_net(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        amount_net = _parse_amount_net(d.pop("amount_net"))

        def _parse_approval_mode(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        approval_mode = _parse_approval_mode(d.pop("approval_mode"))

        asset = d.pop("asset")

        created_at = d.pop("created_at")

        destination_address = d.pop("destination_address")

        external_user_id = d.pop("external_user_id")

        def _parse_failure_reason(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        failure_reason = _parse_failure_reason(d.pop("failure_reason"))

        def _parse_fee(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        fee = _parse_fee(d.pop("fee"))

        status = d.pop("status")

        def _parse_txid(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        txid = _parse_txid(d.pop("txid"))

        updated_at = d.pop("updated_at")

        withdrawal_id = d.pop("withdrawal_id")

        withdrawal_response = cls(
            amount_gross=amount_gross,
            amount_net=amount_net,
            approval_mode=approval_mode,
            asset=asset,
            created_at=created_at,
            destination_address=destination_address,
            external_user_id=external_user_id,
            failure_reason=failure_reason,
            fee=fee,
            status=status,
            txid=txid,
            updated_at=updated_at,
            withdrawal_id=withdrawal_id,
        )

        withdrawal_response.additional_properties = d
        return withdrawal_response

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
