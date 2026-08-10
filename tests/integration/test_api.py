"""The HTTP surface: health, auth wiring, no-store, and the idempotency dependency."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from crypto_processing_api.api.middleware import (
    IdempotencyContext,
    Idempotent,
    require_admin,
    require_readwrite,
)
from crypto_processing_api.core import auth
from crypto_processing_api.db import db_session
from tests.integration.conftest import bearer

IDEMPOTENT_ENDPOINT = "POST /v1/things"


@pytest.fixture(autouse=True)
def probe_routes(app: FastAPI) -> None:
    """Three throwaway routes.

    M2 ships one mutating endpoint, but auth and idempotency are generic
    mechanisms and deserve tests that do not depend on what deposits happen to
    do this milestone.
    """

    @app.get("/_probe/readwrite")
    def probe_readwrite(key: auth.AuthenticatedKey = Depends(require_readwrite)) -> dict[str, str]:
        return {"key_id": key.key_id, "scope": key.scope}

    @app.get("/_probe/admin")
    def probe_admin(key: auth.AuthenticatedKey = Depends(require_admin)) -> dict[str, str]:
        return {"key_id": key.key_id, "scope": key.scope}

    @app.post("/_probe/idempotent")
    def probe_idempotent(
        payload: dict[str, Any],
        context: IdempotencyContext = Depends(Idempotent(IDEMPOTENT_ENDPOINT)),
    ) -> Any:
        if context.is_replay:
            return context.replay_response()
        body = {"created": payload.get("name"), "reclaimed": context.reclaimed}
        context.complete(status_code=201, body=body, resource_id="thing-1")
        context.session.commit()
        return body


def test_healthz_reports_process_and_database(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


def test_healthz_needs_no_api_key(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


def test_healthz_reports_503_when_the_database_is_gone(app: FastAPI, client: TestClient) -> None:
    class BrokenSession:
        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            raise OperationalError("SELECT 1", None, Exception("connection refused"))

    def broken() -> Iterator[Any]:
        yield BrokenSession()

    app.dependency_overrides[db_session] = broken
    try:
        response = client.get("/healthz")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"


def test_every_response_is_no_store(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["cache-control"] == "no-store"


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_missing_key_is_401(client: TestClient) -> None:
    response = client.get("/_probe/readwrite")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_malformed_key_is_401(client: TestClient) -> None:
    assert client.get("/_probe/readwrite", headers=bearer("nonsense")).status_code == 401


def test_valid_key_is_accepted(client: TestClient, readwrite_key: str) -> None:
    response = client.get("/_probe/readwrite", headers=bearer(readwrite_key))
    assert response.status_code == 200
    assert response.json()["scope"] == "readwrite"


def test_readwrite_key_cannot_reach_admin(client: TestClient, readwrite_key: str) -> None:
    response = client.get("/_probe/admin", headers=bearer(readwrite_key))
    assert response.status_code == 403


def test_admin_key_reaches_both(client: TestClient, admin_key: str) -> None:
    assert client.get("/_probe/admin", headers=bearer(admin_key)).status_code == 200
    assert client.get("/_probe/readwrite", headers=bearer(admin_key)).status_code == 200


def test_revoked_key_stops_working(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    assert client.get("/_probe/readwrite", headers=bearer(readwrite_key)).status_code == 200
    auth.revoke_api_key(session, auth.parse_api_key(readwrite_key).key_id)
    session.commit()
    assert client.get("/_probe/readwrite", headers=bearer(readwrite_key)).status_code == 401


def test_error_responses_never_name_the_reason(client: TestClient) -> None:
    body = client.get("/_probe/readwrite", headers=bearer("cpk_test_" + "a" * 43)).json()
    assert body["detail"] == "invalid API key"


def test_idempotent_endpoint_requires_the_header(client: TestClient) -> None:
    response = client.post("/_probe/idempotent", json={"name": "one"})
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


def test_idempotent_endpoint_runs_once_and_replays(client: TestClient) -> None:
    headers = {"Idempotency-Key": "req-1"}
    first = client.post("/_probe/idempotent", json={"name": "one"}, headers=headers)
    assert first.status_code == 200
    assert first.json() == {"created": "one", "reclaimed": False}

    second = client.post("/_probe/idempotent", json={"name": "one"}, headers=headers)
    assert second.status_code == 201
    assert second.json() == {"created": "one", "reclaimed": False}


def test_same_key_different_body_is_422(client: TestClient) -> None:
    headers = {"Idempotency-Key": "req-2"}
    client.post("/_probe/idempotent", json={"name": "one"}, headers=headers)
    response = client.post("/_probe/idempotent", json={"name": "two"}, headers=headers)
    assert response.status_code == 422


def test_in_flight_duplicate_is_409_with_retry_after(client: TestClient, session: Session) -> None:
    """Sent as raw bytes, because the hash is over the bytes on the wire.

    Re-serializing the parsed body would change the whitespace and turn a
    legitimate duplicate into a 422.
    """
    from crypto_processing_api.core import idempotency

    body = b'{"name":"one"}'
    idempotency.begin(session, key="req-3", endpoint=IDEMPOTENT_ENDPOINT, body=body)
    session.commit()

    response = client.post(
        "/_probe/idempotent",
        content=body,
        headers={"Idempotency-Key": "req-3", "Content-Type": "application/json"},
    )
    assert response.status_code == 409
    assert int(response.headers["retry-after"]) > 0


def test_request_hash_is_over_the_bytes_on_the_wire(client: TestClient) -> None:
    """The same JSON with different whitespace is a different body, and must 422."""
    headers = {"Idempotency-Key": "req-4", "Content-Type": "application/json"}
    first = client.post("/_probe/idempotent", content=b'{"name":"one"}', headers=headers)
    assert first.status_code == 200

    second = client.post("/_probe/idempotent", content=b'{"name": "one"}', headers=headers)
    assert second.status_code == 422


def test_openapi_document_builds(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/healthz" in response.json()["paths"]
