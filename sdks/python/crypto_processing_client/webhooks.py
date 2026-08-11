"""Verify and parse a delivered webhook.

This is a port of the server's `core/signing.py`, and the two are held together
by `sdks/signature-vectors.json` — the same cases run against both, so they
cannot quietly disagree.

Three things that are easy to get wrong, and that this module exists so you do
not have to get right yourself:

1. **The signature covers the raw request body bytes.** Parsing the JSON and
   re-serializing it changes the whitespace, and the signature will never
   match. In a framework that parses bodies for you, ask it for the bytes:
   Django `request.body`, Flask `request.get_data()`, FastAPI `await
   request.body()`, Starlette the same.
2. **The comparison is constant time.** `==` on a hex digest leaks how much of
   it was right.
3. **The timestamp is checked.** A signature proves authenticity; only the
   window proves freshness.

Usage::

    event = parse_event(request.body, request.headers, secret=SECRET)
    if event["type"] == "deposit.settled":
        deposit_id = event["data"]["deposit_id"]   # then GET it, see below

And then re-read the resource. A webhook tells you something changed; the GET
tells you what is true.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, cast

from .errors import UnknownEventTypeError, WebhookVerificationError
from .event_types import EVENT_TYPES, PlatformEvent

#: The header the server signs with.
SIGNATURE_HEADER = "X-CPA-Signature"

#: How far the signed timestamp may be from your clock, in seconds. Stripe's
#: convention and what the server enforces on its side.
REPLAY_WINDOW_SECONDS = 300


def _header_value(headers: Mapping[str, str] | str | None) -> str | None:
    """Accept a header mapping or the value itself.

    Every framework spells the headers container differently and most of them
    are case-insensitive; a caller who has only the string should not have to
    build a dict to use this.
    """
    if headers is None or isinstance(headers, str):
        return headers
    for name, value in headers.items():
        if name.lower() == SIGNATURE_HEADER.lower():
            return value
    return None


def verify_webhook(
    body: bytes,
    headers: Mapping[str, str] | str | None,
    secret: str,
    *,
    tolerance_seconds: int = REPLAY_WINDOW_SECONDS,
    now: int | None = None,
) -> bool:
    """True if `body` was signed by `secret` and the signature is still fresh.

    `body` must be the bytes that arrived, not a re-serialization of the
    parsed JSON. `headers` may be the request's header mapping or the value of
    `X-CPA-Signature` on its own.
    """
    if not secret:
        # An unconfigured secret must reject everything. The opposite mistake —
        # treating "no secret" as "no checking" — turns the endpoint into an
        # open one and looks like it is working.
        return False
    header = _header_value(headers)
    if not header:
        return False

    elements = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    raw_timestamp, presented = elements.get("t"), elements.get("v1")
    if not raw_timestamp or not presented:
        return False
    try:
        timestamp = int(raw_timestamp)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False

    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


def parse_event(
    body: bytes,
    headers: Mapping[str, str] | str | None,
    secret: str,
    *,
    tolerance_seconds: int = REPLAY_WINDOW_SECONDS,
    now: int | None = None,
) -> PlatformEvent:
    """Verify, then return the event as a typed dict.

    Raises `WebhookVerificationError` if the signature does not check out —
    answer 401 and do nothing else. Raises `UnknownEventTypeError` for a `type`
    this version does not know, which is the documented "acknowledge and skip"
    case rather than a failure: new event types arrive in any minor release.

    Unknown *fields* are kept, not rejected. Parsing defensively is the
    contract; a validating parser is the thing that breaks on the release that
    adds a field.
    """
    if not verify_webhook(body, headers, secret, tolerance_seconds=tolerance_seconds, now=now):
        raise WebhookVerificationError(
            "the X-CPA-Signature header did not verify against the body and secret"
        )
    return parse_event_unverified(body)


def parse_event_unverified(body: bytes) -> PlatformEvent:
    """Parse an event whose signature you have already checked.

    Separate and awkwardly named on purpose. Verification is not optional, and
    a helper called `parse_event` that skipped it would be the easiest possible
    mistake to make.
    """
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise WebhookVerificationError("the delivered body is not JSON") from exc
    if not isinstance(document, dict):
        raise WebhookVerificationError("the delivered body is not a JSON object")

    envelope = cast(dict[str, Any], document)
    event_type = envelope.get("type")
    if not isinstance(event_type, str):
        raise WebhookVerificationError("the delivered event has no `type`")
    if event_type not in EVENT_TYPES:
        raise UnknownEventTypeError(event_type)
    for field in ("id", "created_at", "data"):
        if field not in envelope:
            raise WebhookVerificationError(f"the delivered event has no `{field}`")
    return cast(PlatformEvent, envelope)
