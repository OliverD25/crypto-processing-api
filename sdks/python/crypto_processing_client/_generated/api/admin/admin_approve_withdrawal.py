from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.approve_withdrawal_request import ApproveWithdrawalRequest
from ...models.error_response import ErrorResponse
from ...models.validation_error_response import ValidationErrorResponse
from ...models.withdrawal_response import WithdrawalResponse
from ...types import Response


def _get_kwargs(
    withdrawal_id: UUID,
    *,
    body: ApproveWithdrawalRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/admin/withdrawals/{withdrawal_id}/approve".format(
            withdrawal_id=quote(str(withdrawal_id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | ValidationErrorResponse | WithdrawalResponse | None:
    if response.status_code == 200:
        response_200 = WithdrawalResponse.from_dict(response.json())

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
        response_422 = ValidationErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    withdrawal_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ApproveWithdrawalRequest,
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalResponse]:
    """Approve a queued withdrawal

    Args:
        withdrawal_id (UUID):
        body (ApproveWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | ValidationErrorResponse | WithdrawalResponse]
    """

    kwargs = _get_kwargs(
        withdrawal_id=withdrawal_id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    withdrawal_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ApproveWithdrawalRequest,
) -> ErrorResponse | ValidationErrorResponse | WithdrawalResponse | None:
    """Approve a queued withdrawal

    Args:
        withdrawal_id (UUID):
        body (ApproveWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | ValidationErrorResponse | WithdrawalResponse
    """

    return sync_detailed(
        withdrawal_id=withdrawal_id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    withdrawal_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ApproveWithdrawalRequest,
) -> Response[ErrorResponse | ValidationErrorResponse | WithdrawalResponse]:
    """Approve a queued withdrawal

    Args:
        withdrawal_id (UUID):
        body (ApproveWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | ValidationErrorResponse | WithdrawalResponse]
    """

    kwargs = _get_kwargs(
        withdrawal_id=withdrawal_id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    withdrawal_id: UUID,
    *,
    client: AuthenticatedClient,
    body: ApproveWithdrawalRequest,
) -> ErrorResponse | ValidationErrorResponse | WithdrawalResponse | None:
    """Approve a queued withdrawal

    Args:
        withdrawal_id (UUID):
        body (ApproveWithdrawalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | ValidationErrorResponse | WithdrawalResponse
    """

    return (
        await asyncio_detailed(
            withdrawal_id=withdrawal_id,
            client=client,
            body=body,
        )
    ).parsed
