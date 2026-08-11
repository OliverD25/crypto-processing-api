from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_deposit_request import CreateDepositRequest
from ...models.deposit_response import DepositResponse
from ...models.deposit_unavailable_response import DepositUnavailableResponse
from ...models.error_response import ErrorResponse
from ...models.validation_error_response import ValidationErrorResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: CreateDepositRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(idempotency_key, Unset):
        headers["Idempotency-Key"] = idempotency_key

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/deposits",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse | None:
    if response.status_code == 201:
        response_201 = DepositResponse.from_dict(response.json())

        return response_201

    if response.status_code == 400:
        response_400 = ErrorResponse.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

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

    if response.status_code == 502:
        response_502 = ErrorResponse.from_dict(response.json())

        return response_502

    if response.status_code == 503:
        response_503 = DepositUnavailableResponse.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[
    DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse
]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateDepositRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> Response[
    DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse
]:
    """Create a deposit and its BTCPay invoice

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateDepositRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse]
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
    body: CreateDepositRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse | None:
    """Create a deposit and its BTCPay invoice

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateDepositRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse
    """

    return sync_detailed(
        client=client,
        body=body,
        idempotency_key=idempotency_key,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateDepositRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> Response[
    DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse
]:
    """Create a deposit and its BTCPay invoice

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateDepositRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse]
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
    body: CreateDepositRequest,
    idempotency_key: None | str | Unset = UNSET,
) -> DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse | None:
    """Create a deposit and its BTCPay invoice

    Args:
        idempotency_key (None | str | Unset): **Required** — omitting it is answered with 400, not
            with a schema error. One key per logical operation, chosen by the caller; a UUID is the
            usual choice. **Retry with the same key.** A retry with a new key is a second deposit or a
            second withdrawal, not a retry. A completed key replays its stored response; a key still
            in flight answers 409 with `Retry-After`; the same key with a different body answers 422.
        body (CreateDepositRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DepositResponse | DepositUnavailableResponse | ErrorResponse | ValidationErrorResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            idempotency_key=idempotency_key,
        )
    ).parsed
