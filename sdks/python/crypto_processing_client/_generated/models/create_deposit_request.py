from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateDepositRequest")


@_attrs_define
class CreateDepositRequest:
    """
    Attributes:
        asset (str):
        external_user_id (str):
        expected_amount (None | str | Unset): Display only, integer smallest units as a string. Top-up invoices credit
            whatever arrives.
    """

    asset: str
    external_user_id: str
    expected_amount: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        asset = self.asset

        external_user_id = self.external_user_id

        expected_amount: None | str | Unset
        if isinstance(self.expected_amount, Unset):
            expected_amount = UNSET
        else:
            expected_amount = self.expected_amount

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "asset": asset,
                "external_user_id": external_user_id,
            }
        )
        if expected_amount is not UNSET:
            field_dict["expected_amount"] = expected_amount

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        asset = d.pop("asset")

        external_user_id = d.pop("external_user_id")

        def _parse_expected_amount(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_amount = _parse_expected_amount(d.pop("expected_amount", UNSET))

        create_deposit_request = cls(
            asset=asset,
            external_user_id=external_user_id,
            expected_amount=expected_amount,
        )

        create_deposit_request.additional_properties = d
        return create_deposit_request

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
