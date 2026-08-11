from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.validation_error_response_detail_type_1_item import (
        ValidationErrorResponseDetailType1Item,
    )


T = TypeVar("T", bound="ValidationErrorResponse")


@_attrs_define
class ValidationErrorResponse:
    """A 422 has two sources and two shapes.

    A business rule (a dust amount, a destination the asset's validator
    refuses, an idempotency key reused with a different body) answers with a
    string. A request that does not fit the request model answers with
    FastAPI's list of field errors.

        Attributes:
            detail (list[ValidationErrorResponseDetailType1Item] | str):
    """

    detail: list[ValidationErrorResponseDetailType1Item] | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        detail: list[dict[str, Any]] | str
        if isinstance(self.detail, list):
            detail = []
            for detail_type_1_item_data in self.detail:
                detail_type_1_item = detail_type_1_item_data.to_dict()
                detail.append(detail_type_1_item)

        else:
            detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "detail": detail,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.validation_error_response_detail_type_1_item import (
            ValidationErrorResponseDetailType1Item,
        )

        d = dict(src_dict)

        def _parse_detail(data: object) -> list[ValidationErrorResponseDetailType1Item] | str:
            try:
                if not isinstance(data, list):
                    raise TypeError()
                detail_type_1 = []
                _detail_type_1 = data
                for detail_type_1_item_data in _detail_type_1:
                    detail_type_1_item = ValidationErrorResponseDetailType1Item.from_dict(
                        detail_type_1_item_data
                    )

                    detail_type_1.append(detail_type_1_item)

                return detail_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ValidationErrorResponseDetailType1Item] | str, data)

        detail = _parse_detail(d.pop("detail"))

        validation_error_response = cls(
            detail=detail,
        )

        validation_error_response.additional_properties = d
        return validation_error_response

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
