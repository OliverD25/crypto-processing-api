from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.admin_withdrawal_response import AdminWithdrawalResponse


T = TypeVar("T", bound="AdminWithdrawalListResponse")


@_attrs_define
class AdminWithdrawalListResponse:
    """
    Attributes:
        withdrawals (list[AdminWithdrawalResponse]):
    """

    withdrawals: list[AdminWithdrawalResponse]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        withdrawals = []
        for withdrawals_item_data in self.withdrawals:
            withdrawals_item = withdrawals_item_data.to_dict()
            withdrawals.append(withdrawals_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "withdrawals": withdrawals,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.admin_withdrawal_response import AdminWithdrawalResponse

        d = dict(src_dict)
        withdrawals = []
        _withdrawals = d.pop("withdrawals")
        for withdrawals_item_data in _withdrawals:
            withdrawals_item = AdminWithdrawalResponse.from_dict(withdrawals_item_data)

            withdrawals.append(withdrawals_item)

        admin_withdrawal_list_response = cls(
            withdrawals=withdrawals,
        )

        admin_withdrawal_list_response.additional_properties = d
        return admin_withdrawal_list_response

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
