from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.address_reservation_response import AddressReservationResponse


T = TypeVar("T", bound="AddressHistoryResponse")


@_attrs_define
class AddressHistoryResponse:
    """Who owned a pooled address, and when. The USDT attribution query.

    Attributes:
        address (None | str):
        reservations (list[AddressReservationResponse]):
    """

    address: None | str
    reservations: list[AddressReservationResponse]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        address: None | str
        address = self.address

        reservations = []
        for reservations_item_data in self.reservations:
            reservations_item = reservations_item_data.to_dict()
            reservations.append(reservations_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "address": address,
                "reservations": reservations,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.address_reservation_response import AddressReservationResponse

        d = dict(src_dict)

        def _parse_address(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        address = _parse_address(d.pop("address"))

        reservations = []
        _reservations = d.pop("reservations")
        for reservations_item_data in _reservations:
            reservations_item = AddressReservationResponse.from_dict(reservations_item_data)

            reservations.append(reservations_item)

        address_history_response = cls(
            address=address,
            reservations=reservations,
        )

        address_history_response.additional_properties = d
        return address_history_response

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
