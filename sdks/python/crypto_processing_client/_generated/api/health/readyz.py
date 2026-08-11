from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ready_response import ReadyResponse
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/readyz",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReadyResponse | None:
    if response.status_code == 200:
        response_200 = ReadyResponse.from_dict(response.json())

        return response_200

    if response.status_code == 503:
        response_503 = ReadyResponse.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReadyResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ReadyResponse]:
    """Component readiness

     Everything `/healthz` deliberately leaves out.

    BTCPay restarts are routine on a shared box, and TronGrid is a third party
    on a free tier. Neither should be able to make this service look down, so
    they are reported here instead, where an operator can look without an
    uptime pinger restarting the API over it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadyResponse]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
) -> ReadyResponse | None:
    """Component readiness

     Everything `/healthz` deliberately leaves out.

    BTCPay restarts are routine on a shared box, and TronGrid is a third party
    on a free tier. Neither should be able to make this service look down, so
    they are reported here instead, where an operator can look without an
    uptime pinger restarting the API over it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadyResponse
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
) -> Response[ReadyResponse]:
    """Component readiness

     Everything `/healthz` deliberately leaves out.

    BTCPay restarts are routine on a shared box, and TronGrid is a third party
    on a free tier. Neither should be able to make this service look down, so
    they are reported here instead, where an operator can look without an
    uptime pinger restarting the API over it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadyResponse]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
) -> ReadyResponse | None:
    """Component readiness

     Everything `/healthz` deliberately leaves out.

    BTCPay restarts are routine on a shared box, and TronGrid is a third party
    on a free tier. Neither should be able to make this service look down, so
    they are reported here instead, where an operator can look without an
    uptime pinger restarting the API over it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadyResponse
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
