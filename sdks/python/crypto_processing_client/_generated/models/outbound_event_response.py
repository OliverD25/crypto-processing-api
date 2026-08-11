from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.outbound_event_response_payload import OutboundEventResponsePayload


T = TypeVar("T", bound="OutboundEventResponse")


@_attrs_define
class OutboundEventResponse:
    """
    Attributes:
        attempts (int):
        created_at (str):
        id (str): The `evt_`-prefixed id the platform dedups on.
        last_error (None | str):
        next_attempt_at (str):
        payload (OutboundEventResponsePayload): The event body. Its shape per event type is in `docs/reference/webhook-
            events.json`.
        raw_id (str):
        status (str): `pending`, `delivered` or `dead`.
        type_ (str):
    """

    attempts: int
    created_at: str
    id: str
    last_error: None | str
    next_attempt_at: str
    payload: OutboundEventResponsePayload
    raw_id: str
    status: str
    type_: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        attempts = self.attempts

        created_at = self.created_at

        id = self.id

        last_error: None | str
        last_error = self.last_error

        next_attempt_at = self.next_attempt_at

        payload = self.payload.to_dict()

        raw_id = self.raw_id

        status = self.status

        type_ = self.type_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "attempts": attempts,
                "created_at": created_at,
                "id": id,
                "last_error": last_error,
                "next_attempt_at": next_attempt_at,
                "payload": payload,
                "raw_id": raw_id,
                "status": status,
                "type": type_,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.outbound_event_response_payload import OutboundEventResponsePayload

        d = dict(src_dict)
        attempts = d.pop("attempts")

        created_at = d.pop("created_at")

        id = d.pop("id")

        def _parse_last_error(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        last_error = _parse_last_error(d.pop("last_error"))

        next_attempt_at = d.pop("next_attempt_at")

        payload = OutboundEventResponsePayload.from_dict(d.pop("payload"))

        raw_id = d.pop("raw_id")

        status = d.pop("status")

        type_ = d.pop("type")

        outbound_event_response = cls(
            attempts=attempts,
            created_at=created_at,
            id=id,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
            payload=payload,
            raw_id=raw_id,
            status=status,
            type_=type_,
        )

        outbound_event_response.additional_properties = d
        return outbound_event_response

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
