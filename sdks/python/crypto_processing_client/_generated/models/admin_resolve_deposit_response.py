from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.deposit_response import DepositResponse


T = TypeVar("T", bound="AdminResolveDepositResponse")


@_attrs_define
class AdminResolveDepositResponse:
    """
    Attributes:
        credited (str): What the server asked BTCPay the payment was worth. There is deliberately no amount field on the
            request.
        deposit (DepositResponse):
    """

    credited: str
    deposit: DepositResponse
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        credited = self.credited

        deposit = self.deposit.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "credited": credited,
                "deposit": deposit,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deposit_response import DepositResponse

        d = dict(src_dict)
        credited = d.pop("credited")

        deposit = DepositResponse.from_dict(d.pop("deposit"))

        admin_resolve_deposit_response = cls(
            credited=credited,
            deposit=deposit,
        )

        admin_resolve_deposit_response.additional_properties = d
        return admin_resolve_deposit_response

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
