from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TransactionResponse")


@_attrs_define
class TransactionResponse:
    """
    Attributes:
        account (str): `user_available` or `user_hold`.
        amount (str):
        asset (str):
        created_at (str):
        direction (str): `credit` or `debit`, from the user's point of view: a deposit reads positive and a withdrawal
            hold negative.
        entry_id (int):
        kind (str):
        memo (None | str):
        posting_id (int):
        source_ref (None | str):
    """

    account: str
    amount: str
    asset: str
    created_at: str
    direction: str
    entry_id: int
    kind: str
    memo: None | str
    posting_id: int
    source_ref: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        account = self.account

        amount = self.amount

        asset = self.asset

        created_at = self.created_at

        direction = self.direction

        entry_id = self.entry_id

        kind = self.kind

        memo: None | str
        memo = self.memo

        posting_id = self.posting_id

        source_ref: None | str
        source_ref = self.source_ref

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "account": account,
                "amount": amount,
                "asset": asset,
                "created_at": created_at,
                "direction": direction,
                "entry_id": entry_id,
                "kind": kind,
                "memo": memo,
                "posting_id": posting_id,
                "source_ref": source_ref,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        account = d.pop("account")

        amount = d.pop("amount")

        asset = d.pop("asset")

        created_at = d.pop("created_at")

        direction = d.pop("direction")

        entry_id = d.pop("entry_id")

        kind = d.pop("kind")

        def _parse_memo(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        memo = _parse_memo(d.pop("memo"))

        posting_id = d.pop("posting_id")

        def _parse_source_ref(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        source_ref = _parse_source_ref(d.pop("source_ref"))

        transaction_response = cls(
            account=account,
            amount=amount,
            asset=asset,
            created_at=created_at,
            direction=direction,
            entry_id=entry_id,
            kind=kind,
            memo=memo,
            posting_id=posting_id,
            source_ref=source_ref,
        )

        transaction_response.additional_properties = d
        return transaction_response

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
