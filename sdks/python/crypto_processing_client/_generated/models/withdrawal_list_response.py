from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.withdrawal_response import WithdrawalResponse


T = TypeVar("T", bound="WithdrawalListResponse")


@_attrs_define
class WithdrawalListResponse:
    """
    Attributes:
        next_cursor (None | str):
        withdrawals (list[WithdrawalResponse]):
    """

    next_cursor: None | str
    withdrawals: list[WithdrawalResponse]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        next_cursor: None | str
        next_cursor = self.next_cursor

        withdrawals = []
        for withdrawals_item_data in self.withdrawals:
            withdrawals_item = withdrawals_item_data.to_dict()
            withdrawals.append(withdrawals_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "next_cursor": next_cursor,
                "withdrawals": withdrawals,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.withdrawal_response import WithdrawalResponse

        d = dict(src_dict)

        def _parse_next_cursor(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor"))

        withdrawals = []
        _withdrawals = d.pop("withdrawals")
        for withdrawals_item_data in _withdrawals:
            withdrawals_item = WithdrawalResponse.from_dict(withdrawals_item_data)

            withdrawals.append(withdrawals_item)

        withdrawal_list_response = cls(
            next_cursor=next_cursor,
            withdrawals=withdrawals,
        )

        withdrawal_list_response.additional_properties = d
        return withdrawal_list_response

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
