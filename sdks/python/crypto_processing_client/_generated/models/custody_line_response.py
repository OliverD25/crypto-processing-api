from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="CustodyLineResponse")


@_attrs_define
class CustodyLineResponse:
    """One asset's answer to: is there still enough on chain to cover what users are owed?

    Attributes:
        asset (str):
        chain_balance (None | str): `null` means the source could not be reached. Not the same as zero.
        chain_source (str):
        difference (None | str): Chain minus what the ledger says. Negative is bad.
        expected_shortfall (str): Derived from in-flight postings, not a tuned epsilon.
        insolvent (bool):
        ledger_custody (str):
        ledger_in_flight (str):
        user_obligations (str):
    """

    asset: str
    chain_balance: None | str
    chain_source: str
    difference: None | str
    expected_shortfall: str
    insolvent: bool
    ledger_custody: str
    ledger_in_flight: str
    user_obligations: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        asset = self.asset

        chain_balance: None | str
        chain_balance = self.chain_balance

        chain_source = self.chain_source

        difference: None | str
        difference = self.difference

        expected_shortfall = self.expected_shortfall

        insolvent = self.insolvent

        ledger_custody = self.ledger_custody

        ledger_in_flight = self.ledger_in_flight

        user_obligations = self.user_obligations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "asset": asset,
                "chain_balance": chain_balance,
                "chain_source": chain_source,
                "difference": difference,
                "expected_shortfall": expected_shortfall,
                "insolvent": insolvent,
                "ledger_custody": ledger_custody,
                "ledger_in_flight": ledger_in_flight,
                "user_obligations": user_obligations,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        asset = d.pop("asset")

        def _parse_chain_balance(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        chain_balance = _parse_chain_balance(d.pop("chain_balance"))

        chain_source = d.pop("chain_source")

        def _parse_difference(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        difference = _parse_difference(d.pop("difference"))

        expected_shortfall = d.pop("expected_shortfall")

        insolvent = d.pop("insolvent")

        ledger_custody = d.pop("ledger_custody")

        ledger_in_flight = d.pop("ledger_in_flight")

        user_obligations = d.pop("user_obligations")

        custody_line_response = cls(
            asset=asset,
            chain_balance=chain_balance,
            chain_source=chain_source,
            difference=difference,
            expected_shortfall=expected_shortfall,
            insolvent=insolvent,
            ledger_custody=ledger_custody,
            ledger_in_flight=ledger_in_flight,
            user_obligations=user_obligations,
        )

        custody_line_response.additional_properties = d
        return custody_line_response

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
