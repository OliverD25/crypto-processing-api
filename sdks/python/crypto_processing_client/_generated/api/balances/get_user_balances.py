from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.balances_response import BalancesResponse
from ...models.error_response import ErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...types import Response


def _get_kwargs(
    external_user_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/users/{external_user_id}/balances".format(
            external_user_id=quote(str(external_user_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> BalancesResponse | ErrorResponse | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = BalancesResponse.from_dict(response.json())

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
) -> Response[BalancesResponse | ErrorResponse | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[BalancesResponse | ErrorResponse | HTTPValidationError]:
    """Per-asset available, held and total

     Per-asset available, held and total.

    User accounts are credit-normal, so their stored balance is negative and
    the sign is flipped here. `held` is not arithmetic on a side table: a hold
    physically moved the money between two accounts, so there is nothing to get
    wrong at read time.

    Args:
        external_user_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BalancesResponse | ErrorResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        external_user_id=external_user_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
) -> BalancesResponse | ErrorResponse | HTTPValidationError | None:
    """Per-asset available, held and total

     Per-asset available, held and total.

    User accounts are credit-normal, so their stored balance is negative and
    the sign is flipped here. `held` is not arithmetic on a side table: a hold
    physically moved the money between two accounts, so there is nothing to get
    wrong at read time.

    Args:
        external_user_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BalancesResponse | ErrorResponse | HTTPValidationError
    """

    return sync_detailed(
        external_user_id=external_user_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
) -> Response[BalancesResponse | ErrorResponse | HTTPValidationError]:
    """Per-asset available, held and total

     Per-asset available, held and total.

    User accounts are credit-normal, so their stored balance is negative and
    the sign is flipped here. `held` is not arithmetic on a side table: a hold
    physically moved the money between two accounts, so there is nothing to get
    wrong at read time.

    Args:
        external_user_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BalancesResponse | ErrorResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        external_user_id=external_user_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
) -> BalancesResponse | ErrorResponse | HTTPValidationError | None:
    """Per-asset available, held and total

     Per-asset available, held and total.

    User accounts are credit-normal, so their stored balance is negative and
    the sign is flipped here. `held` is not arithmetic on a side table: a hold
    physically moved the money between two accounts, so there is nothing to get
    wrong at read time.

    Args:
        external_user_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BalancesResponse | ErrorResponse | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            external_user_id=external_user_id,
            client=client,
        )
    ).parsed
