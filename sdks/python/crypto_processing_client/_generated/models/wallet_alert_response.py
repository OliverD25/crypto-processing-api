from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="WalletAlertResponse")


@_attrs_define
class WalletAlertResponse:
    """
    Attributes:
        amount (str):
        asset (str):
        confirmations (int | None):
        detected_at (str):
        id (int):
        note (None | str):
        txid (str):
    """

    amount: str
    asset: str
    confirmations: int | None
    detected_at: str
    id: int
    note: None | str
    txid: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        amount = self.amount

        asset = self.asset

        confirmations: int | None
        confirmations = self.confirmations

        detected_at = self.detected_at

        id = self.id

        note: None | str
        note = self.note

        txid = self.txid

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "amount": amount,
                "asset": asset,
                "confirmations": confirmations,
                "detected_at": detected_at,
                "id": id,
                "note": note,
                "txid": txid,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        amount = d.pop("amount")

        asset = d.pop("asset")

        def _parse_confirmations(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        confirmations = _parse_confirmations(d.pop("confirmations"))

        detected_at = d.pop("detected_at")

        id = d.pop("id")

        def _parse_note(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        note = _parse_note(d.pop("note"))

        txid = d.pop("txid")

        wallet_alert_response = cls(
            amount=amount,
            asset=asset,
            confirmations=confirmations,
            detected_at=detected_at,
            id=id,
            note=note,
            txid=txid,
        )

        wallet_alert_response.additional_properties = d
        return wallet_alert_response

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
