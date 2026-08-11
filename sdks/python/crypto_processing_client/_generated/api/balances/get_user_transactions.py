from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.error_response import ErrorResponse
from ...models.http_validation_error import HTTPValidationError
from ...models.transactions_response import TransactionsResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    external_user_id: str,
    *,
    asset: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: int | None | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_asset: None | str | Unset
    if isinstance(asset, Unset):
        json_asset = UNSET
    else:
        json_asset = asset
    params["asset"] = json_asset

    params["limit"] = limit

    json_cursor: int | None | Unset
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/users/{external_user_id}/transactions".format(
            external_user_id=quote(str(external_user_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ErrorResponse | HTTPValidationError | TransactionsResponse | None:
    if response.status_code == 200:
        response_200 = TransactionsResponse.from_dict(response.json())

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
) -> Response[ErrorResponse | HTTPValidationError | TransactionsResponse]:
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
    asset: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: int | None | Unset = UNSET,
) -> Response[ErrorResponse | HTTPValidationError | TransactionsResponse]:
    """Ledger history from the user's point of view

     Ledger history from the user's point of view.

    Amounts are negated relative to the stored posting, so a deposit reads as
    positive and a withdrawal hold as negative — the user's perspective, not
    the book's.

    Args:
        external_user_id (str):
        asset (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (int | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | HTTPValidationError | TransactionsResponse]
    """

    kwargs = _get_kwargs(
        external_user_id=external_user_id,
        asset=asset,
        limit=limit,
        cursor=cursor,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
    asset: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: int | None | Unset = UNSET,
) -> ErrorResponse | HTTPValidationError | TransactionsResponse | None:
    """Ledger history from the user's point of view

     Ledger history from the user's point of view.

    Amounts are negated relative to the stored posting, so a deposit reads as
    positive and a withdrawal hold as negative — the user's perspective, not
    the book's.

    Args:
        external_user_id (str):
        asset (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (int | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | HTTPValidationError | TransactionsResponse
    """

    return sync_detailed(
        external_user_id=external_user_id,
        client=client,
        asset=asset,
        limit=limit,
        cursor=cursor,
    ).parsed


async def asyncio_detailed(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
    asset: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: int | None | Unset = UNSET,
) -> Response[ErrorResponse | HTTPValidationError | TransactionsResponse]:
    """Ledger history from the user's point of view

     Ledger history from the user's point of view.

    Amounts are negated relative to the stored posting, so a deposit reads as
    positive and a withdrawal hold as negative — the user's perspective, not
    the book's.

    Args:
        external_user_id (str):
        asset (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (int | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ErrorResponse | HTTPValidationError | TransactionsResponse]
    """

    kwargs = _get_kwargs(
        external_user_id=external_user_id,
        asset=asset,
        limit=limit,
        cursor=cursor,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    external_user_id: str,
    *,
    client: AuthenticatedClient,
    asset: None | str | Unset = UNSET,
    limit: int | Unset = 50,
    cursor: int | None | Unset = UNSET,
) -> ErrorResponse | HTTPValidationError | TransactionsResponse | None:
    """Ledger history from the user's point of view

     Ledger history from the user's point of view.

    Amounts are negated relative to the stored posting, so a deposit reads as
    positive and a withdrawal hold as negative — the user's perspective, not
    the book's.

    Args:
        external_user_id (str):
        asset (None | str | Unset):
        limit (int | Unset):  Default: 50.
        cursor (int | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ErrorResponse | HTTPValidationError | TransactionsResponse
    """

    return (
        await asyncio_detailed(
            external_user_id=external_user_id,
            client=client,
            asset=asset,
            limit=limit,
            cursor=cursor,
        )
    ).parsed
