from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.deposit_response import DepositResponse


T = TypeVar("T", bound="DepositListResponse")


@_attrs_define
class DepositListResponse:
    """
    Attributes:
        deposits (list[DepositResponse]):
        next_cursor (None | str): Keyset cursor. `null` on the last page — a page boundary cannot shift under an insert.
    """

    deposits: list[DepositResponse]
    next_cursor: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deposits = []
        for deposits_item_data in self.deposits:
            deposits_item = deposits_item_data.to_dict()
            deposits.append(deposits_item)

        next_cursor: None | str
        next_cursor = self.next_cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deposits": deposits,
                "next_cursor": next_cursor,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deposit_response import DepositResponse

        d = dict(src_dict)
        deposits = []
        _deposits = d.pop("deposits")
        for deposits_item_data in _deposits:
            deposits_item = DepositResponse.from_dict(deposits_item_data)

            deposits.append(deposits_item)

        def _parse_next_cursor(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))

        deposit_list_response = cls(
            deposits=deposits,
            next_cursor=next_cursor,
        )

        deposit_list_response.additional_properties = d
        return deposit_list_response

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
