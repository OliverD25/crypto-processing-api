from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_withdrawal_request import CreateWithdrawalRequest
from ...models.error_response import ErrorResponse
from ...models.validation_error_response import ValidationErrorResponse
from ...models.withdrawal_created_response import WithdrawalCreatedResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: CreateWithdrawalRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/withdrawals",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse | None:
    if response.status_code == 201:
        response_201 = WithdrawalCreatedResponse.from_dict(response.json())

        return response_201

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 402:
        response_402 = ErrorResponse.from_dict(response.json())

        return response_402

    if response.status_code == 403:
        response_403 = ErrorResponse.from_dict(response.json())

        return response_403

    if response.status_code == 404:
        response_404 = ErrorResponse.from_dict(response.json())

        return response_404

    if response.status_code == 409:
        response_409 = ErrorResponse.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = ValidationErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateWithdrawalRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse]:
    """Request a withdrawal and reserve the balance

     The hold is placed before the response returns, so `pending_approval` is not a rejection — the funds
    are already reserved.

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: CreateWithdrawalRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse | None:
    """Request a withdrawal and reserve the balance

     The hold is placed before the response returns, so `pending_approval` is not a rejection — the funds
    are already reserved.

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse
    """

    return sync_detailed(
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateWithdrawalRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse]:
    """Request a withdrawal and reserve the balance

     The hold is placed before the response returns, so `pending_approval` is not a rejection — the funds
    are already reserved.

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse]
    """

    kwargs = _get_kwargs(
        body=body,
        idempotency_key=idempotency_key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: CreateWithdrawalRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse | None:
    """Request a withdrawal and reserve the balance

     The hold is placed before the response returns, so `pending_approval` is not a rejection — the funds
    are already reserved.

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | ValidationErrorResponse | WithdrawalCreatedResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
