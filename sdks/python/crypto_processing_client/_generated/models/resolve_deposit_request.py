from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.resolve_deposit_request_action import (
    ResolveDepositRequestAction,
    check_resolve_deposit_request_action,
)
from ..types import UNSET, Unset

T = TypeVar("T", bound="ResolveDepositRequest")


@_attrs_define
class ResolveDepositRequest:
    """No amount field, deliberately.

    The whole deposit pipeline insists that a webhook payload is never an
    amount source; a human typing one at 2am is not a better source. The admin
    confirms attribution — which deposit, which payment — and the server asks
    BTCPay what that payment was worth.

        Attributes:
            action (ResolveDepositRequestAction):
            payment_id (None | str | Unset):
    """

    action: ResolveDepositRequestAction
    payment_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        action: str = self.action

        payment_id: None | str | Unset
        if isinstance(self.payment_id, Unset):
            payment_id = UNSET
        else:
            payment_id = self.payment_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "action": action,
            }
        )
        if payment_id is not UNSET:
            field_dict["payment_id"] = payment_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = check_resolve_deposit_request_action(d.pop("action"))

        def _parse_payment_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        payment_id = _parse_payment_id(d.pop("payment_id", UNSET))

        resolve_deposit_request = cls(
            action=action,
            payment_id=payment_id,
        )

        resolve_deposit_request.additional_properties = d
        return resolve_deposit_request

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
