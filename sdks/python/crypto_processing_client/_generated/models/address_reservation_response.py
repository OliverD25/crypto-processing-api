from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AddressReservationResponse")


@_attrs_define
class AddressReservationResponse:
    """
    Attributes:
        deposit_id (str):
        external_user_id (str):
        reserved_from (None | str):
        reserved_until (None | str):
        status (str):
    """

    deposit_id: str
    external_user_id: str
    reserved_from: None | str
    reserved_until: None | str
    status: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deposit_id = self.deposit_id

        external_user_id = self.external_user_id

        reserved_from: None | str
        reserved_from = self.reserved_from

        reserved_until: None | str
        reserved_until = self.reserved_until

        status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deposit_id": deposit_id,
                "external_user_id": external_user_id,
                "reserved_from": reserved_from,
                "reserved_until": reserved_until,
                "status": status,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deposit_id = d.pop("deposit_id")

        external_user_id = d.pop("external_user_id")

        def _parse_reserved_from(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reserved_from = _parse_reserved_from(d.pop("reserved_from"))

        def _parse_reserved_until(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reserved_until = _parse_reserved_until(d.pop("reserved_until"))

        status = d.pop("status")

        address_reservation_response = cls(
            deposit_id=deposit_id,
            external_user_id=external_user_id,
            reserved_from=reserved_from,
            reserved_until=reserved_until,
            status=status,
        )

        address_reservation_response.additional_properties = d
        return address_reservation_response

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
