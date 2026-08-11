from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.custody_line_response import CustodyLineResponse


T = TypeVar("T", bound="ReconciliationResponse")


@_attrs_define
class ReconciliationResponse:
    """
    Attributes:
        alerts (list[str]):
        custody (list[CustodyLineResponse]):
        healthy (bool):
        ledger_consistent (bool):
        materialized_vs_derived_drifts (list[str]):
        unbalanced_entries (list[str]):
    """

    alerts: list[str]
    custody: list[CustodyLineResponse]
    healthy: bool
    ledger_consistent: bool
    materialized_vs_derived_drifts: list[str]
    unbalanced_entries: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        alerts = self.alerts

        custody = []
        for custody_item_data in self.custody:
            custody_item = custody_item_data.to_dict()
            custody.append(custody_item)

        healthy = self.healthy

        ledger_consistent = self.ledger_consistent

        materialized_vs_derived_drifts = self.materialized_vs_derived_drifts

        unbalanced_entries = self.unbalanced_entries

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "alerts": alerts,
                "custody": custody,
                "healthy": healthy,
                "ledger_consistent": ledger_consistent,
                "materialized_vs_derived_drifts": materialized_vs_derived_drifts,
                "unbalanced_entries": unbalanced_entries,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.custody_line_response import CustodyLineResponse

        d = dict(src_dict)
        alerts = cast(list[str], d.pop("alerts"))

        custody = []
        _custody = d.pop("custody")
        for custody_item_data in _custody:
            custody_item = CustodyLineResponse.from_dict(custody_item_data)

            custody.append(custody_item)

        healthy = d.pop("healthy")

        ledger_consistent = d.pop("ledger_consistent")

        materialized_vs_derived_drifts = cast(list[str], d.pop("materialized_vs_derived_drifts"))

        unbalanced_entries = cast(list[str], d.pop("unbalanced_entries"))

        reconciliation_response = cls(
            alerts=alerts,
            custody=custody,
            healthy=healthy,
            ledger_consistent=ledger_consistent,
            materialized_vs_derived_drifts=materialized_vs_derived_drifts,
            unbalanced_entries=unbalanced_entries,
        )

        reconciliation_response.additional_properties = d
        return reconciliation_response

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
