from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="BalanceResponse")


@_attrs_define
class BalanceResponse:
    """
    Attributes:
        asset (str):
        available (str):
        held (str): Reserved by a withdrawal. Held money physically sits in a different ledger account, so this is not
            arithmetic on a side table.
        total (str):
    """

    asset: str
    available: str
    held: str
    total: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        asset = self.asset

        available = self.available

        held = self.held

        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "asset": asset,
                "available": available,
                "held": held,
                "total": total,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        asset = d.pop("asset")

        available = d.pop("available")

        held = d.pop("held")

        total = d.pop("total")

        balance_response = cls(
            asset=asset,
            available=available,
            held=held,
            total=total,
        )

        balance_response.additional_properties = d
        return balance_response

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
