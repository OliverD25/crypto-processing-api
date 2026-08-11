from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.balance_response import BalanceResponse


T = TypeVar("T", bound="BalancesResponse")


@_attrs_define
class BalancesResponse:
    """
    Attributes:
        balances (list[BalanceResponse]):
        external_user_id (str):
    """

    balances: list[BalanceResponse]
    external_user_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        balances = []
        for balances_item_data in self.balances:
            balances_item = balances_item_data.to_dict()
            balances.append(balances_item)

        external_user_id = self.external_user_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "balances": balances,
                "external_user_id": external_user_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.balance_response import BalanceResponse

        d = dict(src_dict)
        balances = []
        _balances = d.pop("balances")
        for balances_item_data in _balances:
            balances_item = BalanceResponse.from_dict(balances_item_data)

            balances.append(balances_item)

        external_user_id = d.pop("external_user_id")

        balances_response = cls(
            balances=balances,
            external_user_id=external_user_id,
        )

        balances_response.additional_properties = d
        return balances_response

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
