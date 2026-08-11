from typing import Literal

ResolveDepositRequestAction = Literal["credit", "dismiss"]

RESOLVE_DEPOSIT_REQUEST_ACTION_VALUES: set[ResolveDepositRequestAction] = {
    "credit",
    "dismiss",
}


def check_resolve_deposit_request_action(value: str) -> ResolveDepositRequestAction:
    if value in RESOLVE_DEPOSIT_REQUEST_ACTION_VALUES:
        return value
    raise TypeError(
        f"Unexpected value {value!r}. Expected one of {RESOLVE_DEPOSIT_REQUEST_ACTION_VALUES!r}"
    )
