"""BTCPay webhook ingress.

Ack then process. The endpoint verifies the signature, makes the event durable
and returns 200 — it never touches the ledger. BTCPay's redelivery budget is
about eight attempts over an hour, which is far too small to be our retry
mechanism, so our own worker owns retries over `webhook_events` rows.

The only non-200 answers are 401 for a bad signature and 400 for a body that
will not parse. Everything else — unknown event type, unknown invoice, a
handler that will later fail — is 200, stored, and dealt with by the worker.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crypto_processing_api.api.middleware import get_settings_dependency
from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.core.signing import BTCPAY_SIGNATURE_HEADER, verify_btcpay_signature
from crypto_processing_api.db import db_session
from crypto_processing_api.ledger.models import WebhookEvent
from crypto_processing_api.services.deposits import is_cpapi_invoice

router = APIRouter(tags=["webhooks"])
logger = get_logger(__name__)


@router.post("/webhooks/btcpay", status_code=status.HTTP_200_OK, response_model=None)
async def btcpay_webhook(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings_dependency)],
) -> dict[str, Any]:
    # Raw bytes first. The HMAC covers exactly what arrived; a parsed and
    # re-serialized body has different whitespace and will never match.
    raw = await request.body()

    if not verify_btcpay_signature(
        settings.btcpay_webhook_secret or "", raw, request.headers.get(BTCPAY_SIGNATURE_HEADER)
    ):
        # No body in the log: an attacker must not be able to write into it.
        logger.warning(
            "webhook.bad_signature",
            client=request.client.host if request.client else None,
            bytes=len(raw),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unparseable body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    delivery_id = payload.get("deliveryId")
    event_type = payload.get("type")
    if not isinstance(delivery_id, str) or not isinstance(event_type, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing deliveryId or type")

    # Redeliveries of one event share originalDeliveryId, so this key makes a
    # retry storm collapse to a single row.
    original = payload.get("originalDeliveryId")
    dedup_key = original if isinstance(original, str) and original else delivery_id

    store_id = payload.get("storeId")
    if (
        settings.btcpay_store_id
        and isinstance(store_id, str)
        and store_id != settings.btcpay_store_id
    ):
        logger.info("webhook.other_store", event_type=event_type, store=store_id)
        return {"status": "ignored", "reason": "different store"}

    metadata = payload.get("metadata")
    if (
        event_type.startswith("Invoice")
        and isinstance(metadata, dict)
        and not is_cpapi_invoice(metadata)
    ):
        # Someone else is using this store. Their invoices are none of our
        # business, and crediting from one would be a fabricated deposit.
        logger.info("webhook.not_cpapi", event_type=event_type, invoice=payload.get("invoiceId"))
        return {"status": "ignored", "reason": "not a cpapi invoice"}

    session.execute(
        pg_insert(WebhookEvent)
        .values(
            dedup_key=dedup_key,
            delivery_id=delivery_id,
            event_type=event_type,
            btcpay_invoice_id=payload.get("invoiceId"),
            btcpay_payout_id=payload.get("payoutId"),
            payload=payload,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )
    session.commit()

    logger.info(
        "webhook.accepted",
        event_type=event_type,
        dedup_key=dedup_key,
        redelivery=bool(payload.get("isRedelivery")),
    )
    return {"status": "accepted"}
