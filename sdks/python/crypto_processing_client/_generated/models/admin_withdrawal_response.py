from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AdminWithdrawalResponse")


@_attrs_define
class AdminWithdrawalResponse:
    """The queue view. Everything above plus who decided what.

    Attributes:
        amount_gross (str):
        amount_net (None | str): `null` until submission.
        approval_mode (None | str): `auto` or `manual`.
        approved_by (None | str):
        asset (str):
        backend_ref (None | str):
        created_at (str):
        destination_address (str):
        external_user_id (str):
        failure_reason (None | str):
        fee (None | str): `null` until submission, when the fee is fixed.
        rejected_by (None | str):
        release_attestation (None | str):
        released_by (None | str):
        status (str): requested, pending_approval, approved, rejected, submitting, submitted, broadcast, confirmed,
            failed, refunded.
        txid (None | str):
        updated_at (str):
        withdrawal_id (str):
    """

    amount_gross: str
    amount_net: None | str
    approval_mode: None | str
    approved_by: None | str
    asset: str
    backend_ref: None | str
    created_at: str
    destination_address: str
    external_user_id: str
    failure_reason: None | str
    fee: None | str
    rejected_by: None | str
    release_attestation: None | str
    released_by: None | str
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

        approved_by: None | str
        approved_by = self.approved_by

        asset = self.asset

        backend_ref: None | str
        backend_ref = self.backend_ref

        created_at = self.created_at

        destination_address = self.destination_address

        external_user_id = self.external_user_id

        failure_reason: None | str
        failure_reason = self.failure_reason

        fee: None | str
        fee = self.fee

        rejected_by: None | str
        rejected_by = self.rejected_by

        release_attestation: None | str
        release_attestation = self.release_attestation

        released_by: None | str
        released_by = self.released_by

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
                "approved_by": approved_by,
                "asset": asset,
                "backend_ref": backend_ref,
                "created_at": created_at,
                "destination_address": destination_address,
                "external_user_id": external_user_id,
                "failure_reason": failure_reason,
                "fee": fee,
                "rejected_by": rejected_by,
                "release_attestation": release_attestation,
                "released_by": released_by,
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

        def _parse_approved_by(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        approved_by = _parse_approved_by(d.pop("approved_by"))

        asset = d.pop("asset")

        def _parse_backend_ref(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        backend_ref = _parse_backend_ref(d.pop("backend_ref"))

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

        def _parse_rejected_by(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        rejected_by = _parse_rejected_by(d.pop("rejected_by"))

        def _parse_release_attestation(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        release_attestation = _parse_release_attestation(d.pop("release_attestation"))

        def _parse_released_by(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        released_by = _parse_released_by(d.pop("released_by"))

        status = d.pop("status")

        def _parse_txid(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        txid = _parse_txid(d.pop("txid"))

        updated_at = d.pop("updated_at")

        withdrawal_id = d.pop("withdrawal_id")

        admin_withdrawal_response = cls(
            amount_gross=amount_gross,
            amount_net=amount_net,
            approval_mode=approval_mode,
            approved_by=approved_by,
            asset=asset,
            backend_ref=backend_ref,
            created_at=created_at,
            destination_address=destination_address,
            external_user_id=external_user_id,
            failure_reason=failure_reason,
            fee=fee,
            rejected_by=rejected_by,
            release_attestation=release_attestation,
            released_by=released_by,
            status=status,
            txid=txid,
            updated_at=updated_at,
            withdrawal_id=withdrawal_id,
        )

        admin_withdrawal_response.additional_properties = d
        return admin_withdrawal_response

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
