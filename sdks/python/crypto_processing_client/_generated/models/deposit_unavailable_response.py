from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.deposit_unavailable_detail import DepositUnavailableDetail


T = TypeVar("T", bound="DepositUnavailableResponse")


@_attrs_define
class DepositUnavailableResponse:
    """Deposit creation answers 503 with two different shapes.

    Pool exhaustion carries the structured detail above. An unreachable BTCPay
    (`deposits.py`, the ambiguous `BTCPayError` branch) carries a plain string,
    because there is nothing machine-readable to say about it. Both are
    retryable with the same key.

    The union is here because a generated client parses this schema literally:
    typed as the structured detail alone, every SDK crashes on the string
    branch — which is the branch that fires when BTCPay is down, i.e. exactly
    when the platform most needs a usable error.

        Attributes:
            detail (DepositUnavailableDetail | str):
    """

    detail: DepositUnavailableDetail | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.deposit_unavailable_detail import DepositUnavailableDetail

        detail: dict[str, Any] | str
        if isinstance(self.detail, DepositUnavailableDetail):
            detail = self.detail.to_dict()
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
        from ..models.deposit_unavailable_detail import DepositUnavailableDetail

        d = dict(src_dict)

        def _parse_detail(data: object) -> DepositUnavailableDetail | str:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                detail_type_0 = DepositUnavailableDetail.from_dict(data)

                return detail_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DepositUnavailableDetail | str, data)

        detail = _parse_detail(d.pop("detail"))

        deposit_unavailable_response = cls(
            detail=detail,
        )

        deposit_unavailable_response.additional_properties = d
        return deposit_unavailable_response

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
