from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.admin_resolve_deposit_response import AdminResolveDepositResponse
from ...models.error_response import ErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...models.resolve_deposit_request import ResolveDepositRequest
from ...types import Response


def _get_kwargs(
    deposit_id: UUID,
    *,
    body: ResolveDepositRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/admin/deposits/{deposit_id}/resolve".format(
            deposit_id=quote(str(deposit_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AdminResolveDepositResponse | ErrorResponse | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = AdminResolveDepositResponse.from_dict(response.json())

        return response_200

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
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if response.status_code == 503:
        response_503 = ErrorResponse.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[AdminResolveDepositResponse | ErrorResponse | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    deposit_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ResolveDepositRequest,
) -> Response[AdminResolveDepositResponse | ErrorResponse | HTTPValidationError]:
    """Credit or dismiss a deposit under review

    Args:
        deposit_id (UUID):
        body (ResolveDepositRequest): No amount field, deliberately.

            The whole deposit pipeline insists that a webhook payload is never an
            amount source; a human typing one at 2am is not a better source. The admin
            confirms attribution — which deposit, which payment — and the server asks
            BTCPay what that payment was worth.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AdminResolveDepositResponse | ErrorResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        deposit_id=deposit_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    deposit_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ResolveDepositRequest,
) -> AdminResolveDepositResponse | ErrorResponse | HTTPValidationError | None:
    """Credit or dismiss a deposit under review

    Args:
        deposit_id (UUID):
        body (ResolveDepositRequest): No amount field, deliberately.

            The whole deposit pipeline insists that a webhook payload is never an
            amount source; a human typing one at 2am is not a better source. The admin
            confirms attribution — which deposit, which payment — and the server asks
            BTCPay what that payment was worth.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AdminResolveDepositResponse | ErrorResponse | HTTPValidationError
    """

    return sync_detailed(
        deposit_id=deposit_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    deposit_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ResolveDepositRequest,
) -> Response[AdminResolveDepositResponse | ErrorResponse | HTTPValidationError]:
    """Credit or dismiss a deposit under review

    Args:
        deposit_id (UUID):
        body (ResolveDepositRequest): No amount field, deliberately.

            The whole deposit pipeline insists that a webhook payload is never an
            amount source; a human typing one at 2am is not a better source. The admin
            confirms attribution — which deposit, which payment — and the server asks
            BTCPay what that payment was worth.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AdminResolveDepositResponse | ErrorResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        deposit_id=deposit_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    deposit_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ResolveDepositRequest,
) -> AdminResolveDepositResponse | ErrorResponse | HTTPValidationError | None:
    """Credit or dismiss a deposit under review

    Args:
        deposit_id (UUID):
        body (ResolveDepositRequest): No amount field, deliberately.

            The whole deposit pipeline insists that a webhook payload is never an
            amount source; a human typing one at 2am is not a better source. The admin
            confirms attribution — which deposit, which payment — and the server asks
            BTCPay what that payment was worth.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AdminResolveDepositResponse | ErrorResponse | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            deposit_id=deposit_id,
            client=client,
            body=body,
        )
    ).parsed
