from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...models.wallet_alerts_response import WalletAlertsResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    limit: int | Unset = 50,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/admin/wallet-alerts",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | HTTPValidationError | WalletAlertsResponse | None:
    if response.status_code == 200:
        response_200 = WalletAlertsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = ErrorResponse.from_dict(response.json())

        return response_401

    if response.status_code == 403:
        response_403 = ErrorResponse.from_dict(response.json())

        return response_403

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | HTTPValidationError | WalletAlertsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 50,
) -> Response[ErrorResponse | HTTPValidationError | WalletAlertsResponse]:
    """Wallet receives that match no deposit payment

     Wallet receives that match no deposit payment.

    These are coins in custody that no user has been credited for — usually a
    payment to an address BTCPay has stopped watching. Job C's aggregate check
    cannot see them, because extra money in the wallet reads as healthy.

    Args:
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | HTTPValidationError | WalletAlertsResponse]
    """

    kwargs = _get_kwargs(
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 50,
) -> ErrorResponse | HTTPValidationError | WalletAlertsResponse | None:
    """Wallet receives that match no deposit payment

     Wallet receives that match no deposit payment.

    These are coins in custody that no user has been credited for — usually a
    payment to an address BTCPay has stopped watching. Job C's aggregate check
    cannot see them, because extra money in the wallet reads as healthy.

    Args:
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | HTTPValidationError | WalletAlertsResponse
    """

    return sync_detailed(
        client=client,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 50,
) -> Response[ErrorResponse | HTTPValidationError | WalletAlertsResponse]:
    """Wallet receives that match no deposit payment

     Wallet receives that match no deposit payment.

    These are coins in custody that no user has been credited for — usually a
    payment to an address BTCPay has stopped watching. Job C's aggregate check
    cannot see them, because extra money in the wallet reads as healthy.

    Args:
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | HTTPValidationError | WalletAlertsResponse]
    """

    kwargs = _get_kwargs(
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    limit: int | Unset = 50,
) -> ErrorResponse | HTTPValidationError | WalletAlertsResponse | None:
    """Wallet receives that match no deposit payment

     Wallet receives that match no deposit payment.

    These are coins in custody that no user has been credited for — usually a
    payment to an address BTCPay has stopped watching. Job C's aggregate check
    cannot see them, because extra money in the wallet reads as healthy.

    Args:
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | HTTPValidationError | WalletAlertsResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            limit=limit,
        )
    ).parsed
