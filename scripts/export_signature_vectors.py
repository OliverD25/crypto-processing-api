"""Write the cross-language webhook signature vectors to `sdks/`.

The outbound signature scheme has three implementations: this server's
(`core/signing.py`), the Python SDK's and the TypeScript SDK's. Three
implementations of an HMAC scheme drift silently — each one passes its own
tests, and the disagreement only ever shows up as an integrator's endpoint
rejecting real traffic.

So all three assert against one committed file of vectors, and that file is
produced here by calling the server's own `sign_platform_payload`. Never by
hand: a file somebody has corrected has stopped being evidence.

The event bodies in it are built through the payload models in
`services/events.py` and rendered by the same `event_body` that the delivery
worker signs, so they are the real wire bytes rather than a plausible
imitation. That makes the file the SDKs' parser fixture as well as their
signature fixture.

Deterministic on purpose — sorted keys, fixed indent, one trailing newline —
because a diff that changes on every run is a gate nobody can read.

    python scripts/export_signature_vectors.py           # write
    python scripts/export_signature_vectors.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

#: Importing the package reads the settings at module scope in a few places.
#: Nothing here connects; the value only has to parse.
PLACEHOLDER_DATABASE_URL = "postgresql+psycopg://spec:spec@localhost:5432/spec"
os.environ.setdefault("DATABASE_URL", PLACEHOLDER_DATABASE_URL)

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_PATH = REPO_ROOT / "sdks" / "signature-vectors.json"

#: A fixture, not a credential. Deliberately words rather than entropy, so that
#: nobody reading a diff has to wonder whether a real secret escaped.
SECRET = "not-a-real-secret-webhook-signature-vectors"
OTHER_SECRET = "not-a-real-secret-some-other-platform"

#: Three shapes of unix timestamp: an ordinary one, the smallest that is not
#: zero, and 2**31-1. A client that parses the timestamp into a 32-bit int, or
#: assumes ten digits, or reads milliseconds, fails on one of these and passes
#: the other two.
TIMESTAMPS = (1760000000, 1, 2147483647)

#: Fixed so the file does not change when the clock does.
EVENT_TIMESTAMP = 1760000000
EVENT_CREATED_AT = datetime(2026, 8, 10, 13, 11, 2, tzinfo=UTC)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _body_entry(description: str, raw: bytes) -> dict[str, Any]:
    """One body, base64 first.

    `base64` is authoritative and always present. `utf8` is a convenience for
    the reader and for a test that wants a literal, and it is only there when
    the bytes really are UTF-8 — one body deliberately is not, because an
    implementation that round-trips the payload through a string instead of
    holding the raw bytes must fail somewhere visible.
    """
    entry: dict[str, Any] = {"description": description, "base64": _b64(raw)}
    with contextlib.suppress(UnicodeDecodeError):
        entry["utf8"] = raw.decode("utf-8")
    return entry


def _event_bodies() -> dict[str, bytes]:
    """The eight real envelopes, rendered by the worker's own serializer."""
    from crypto_processing_api.ledger.models import OutboundEvent
    from crypto_processing_api.services import events
    from crypto_processing_api.workers.outbound_delivery import event_body

    deposit = events.DepositEventData(
        deposit_id="019feb96-7e52-771a-a8cb-a86dccc87339",
        external_user_id="user-42",
        asset="BTC",
        status="settled",
        amount_credited="0.50000000",
    )
    detected = events.DepositDetectedData(
        **deposit.model_dump() | {"status": "confirming", "amount_credited": "0.00000000"},
        payments=["p-1"],
    )
    withdrawal = events.WithdrawalEventData(
        withdrawal_id="019feb97-1c04-7b2e-9f3a-2d5c7e8b1a06",
        external_user_id="user-42",
        asset="BTC",
        status="broadcast",
        amount_gross="0.25000000",
        amount_net="0.24990000",
        fee="0.00010000",
        destination_address="bcrt1qdcaqy5dph55w0nyfg2zdu7nkrmzwpkwsej29yq",
        txid="4f2a9c1d8b3e5a7f0c2d4e6a8b0c2d4e6a8b0c2d4e6a8b0c2d4e6a8b0c2d4e6a",
    )

    def decision(status: str, reason: str) -> events.WithdrawalDecisionData:
        return events.WithdrawalDecisionData(
            **withdrawal.model_dump() | {"status": status}, reason=reason
        )

    payloads: dict[str, Any] = {
        events.DEPOSIT_DETECTED: detected,
        events.DEPOSIT_SETTLED: deposit,
        events.DEPOSIT_REVIEW: deposit.model_copy(update={"status": "review"}),
        events.DEPOSIT_EXPIRED: deposit.model_copy(
            update={"status": "expired", "amount_credited": "0.00000000"}
        ),
        events.WITHDRAWAL_PENDING_APPROVAL: decision(
            "pending_approval", "above the per-withdrawal approval threshold"
        ),
        events.WITHDRAWAL_BROADCAST: withdrawal,
        events.WITHDRAWAL_COMPLETED: withdrawal.model_copy(update={"status": "completed"}),
        events.WITHDRAWAL_FAILED: decision(
            "failed", "the payout was cancelled by the operator; the hold is still in place"
        ),
    }
    assert set(payloads) == set(events.EVENT_TYPES), "an event type has no sample body"

    bodies: dict[str, bytes] = {}
    for index, (event_type, payload) in enumerate(payloads.items()):
        row = OutboundEvent(
            # Deterministic and shaped like a uuid7, because a sample the SDKs
            # parse should look like something they will really receive.
            id=UUID(f"019f3c1e-0a2b-7c4d-8e5f-6a7b8c9d0e{index:02x}"),
            event_type=event_type,
            payload=payload.model_dump(),
            created_at=EVENT_CREATED_AT,
        )
        raw = event_body(row)
        # Proves the sample is not just plausible: it satisfies the same
        # discriminated union the SDKs generate their parser from.
        events.PLATFORM_EVENT_ADAPTER.validate_json(raw)
        bodies[event_type] = raw
    return bodies


def build_document() -> dict[str, Any]:
    from crypto_processing_api.core.signing import (
        PLATFORM_SIGNATURE_HEADER,
        REPLAY_WINDOW_SECONDS,
        sign_platform_payload,
    )

    raw_bodies: dict[str, bytes] = {
        "ascii": b'{"id":"evt_1","type":"deposit.settled","amount":"0.50000000"}',
        "unicode": '{"memo":"платіж отримано ✓","asset":"USDT_TRC20"}'.encode(),
        "quotes-and-backslashes": rb'{"memo":"he said \"paid\" \\ then left"}',
        "empty": b"",
        # 8 KiB. Long enough that an implementation which hashes only the first
        # chunk, or which builds the signed string by concatenating into a
        # fixed buffer, gets a different digest here and the same one elsewhere.
        "large": b'{"filler":"' + b"cpa" * 2730 + b'"}',
        # Not valid UTF-8, on purpose. The signature covers bytes.
        "binary": bytes(range(256)),
    }
    descriptions = {
        "ascii": "A plain ASCII JSON body, the ordinary case.",
        "unicode": "Non-ASCII characters, so UTF-8 encoding is pinned rather than assumed.",
        "quotes-and-backslashes": "An embedded quote and a backslash, where naive string "
        "concatenation breaks.",
        "empty": "No body at all. An implementation that treats empty as unsigned fails here.",
        "large": "8 KiB, past the point where a chunked or truncated hash would still agree.",
        "binary": "Bytes that are not valid UTF-8. The scheme signs bytes, never a decoded "
        "string; this body has no `utf8` field for that reason.",
    }

    for event_type, raw in _event_bodies().items():
        key = f"event.{event_type}"
        raw_bodies[key] = raw
        descriptions[key] = (
            f"A real `{event_type}` envelope, rendered by the delivery worker's own "
            "serializer. Also the parser fixture for that event type."
        )

    # Referenced by the tampering case, never signed — it is what the attacker
    # substituted, so a signature over it would defeat the point.
    extra_bodies = {
        "ascii-tampered": _body_entry(
            "The `ascii` body with one digit of the amount changed, for the tampering case.",
            raw_bodies["ascii"].replace(b"0.50000000", b"0.50000001"),
        )
    }

    bodies = dict(
        sorted(
            (
                {name: _body_entry(descriptions[name], raw) for name, raw in raw_bodies.items()}
                | extra_bodies
            ).items()
        )
    )

    sign: list[dict[str, Any]] = []
    for name in raw_bodies:
        # The generic bodies go against every timestamp, which is what proves
        # the timestamp is inside the signed string. The event samples are
        # about payload shape, so one timestamp each is enough.
        stamps = (EVENT_TIMESTAMP,) if name.startswith("event.") else TIMESTAMPS
        for timestamp in stamps:
            sign.append(
                {
                    "name": f"{name}@{timestamp}",
                    "body": name,
                    "secret": SECRET,
                    "timestamp": timestamp,
                    "signature": sign_platform_payload(
                        SECRET, raw_bodies[name], timestamp=timestamp
                    ),
                }
            )

    verify = _verify_cases(raw_bodies, sign_platform_payload)

    return {
        "$comment": (
            "Generated by scripts/export_signature_vectors.py from the server's own signer. "
            "Never edit by hand: a corrected vector has stopped being evidence. "
            "The secrets are fixtures, not credentials."
        ),
        "scheme": {
            "header": PLATFORM_SIGNATURE_HEADER,
            "format": "t=<unix seconds>,v1=<lowercase hex>",
            "signed_bytes": 'the ASCII of "{t}." followed by the raw body bytes',
            "algorithm": "HMAC-SHA256, hex digest, compared in constant time",
            "replay_window_seconds": REPLAY_WINDOW_SECONDS,
            "reference_implementation": "src/crypto_processing_api/core/signing.py",
        },
        "bodies": bodies,
        "sign": sign,
        "verify": verify,
    }


def _verify_cases(
    raw_bodies: dict[str, bytes],
    sign_platform_payload: Any,
) -> list[dict[str, Any]]:
    """Cases for the verifier, including the ones that must be refused.

    A vector file of passing cases only would be satisfied by a verifier that
    returns true unconditionally, which is the exact bug worth catching.
    """
    window = 300
    base_t = 1760000000
    good = sign_platform_payload(SECRET, raw_bodies["ascii"], timestamp=base_t)
    digest = good.split("v1=", 1)[1]

    cases: list[dict[str, Any]] = [
        {
            "name": "valid-at-signing-time",
            "body": "ascii",
            "secret": SECRET,
            "header": good,
            "now": base_t,
            "valid": True,
            "why": "the ordinary case.",
        },
        {
            "name": "valid-at-the-late-edge",
            "body": "ascii",
            "secret": SECRET,
            "header": good,
            "now": base_t + window,
            "valid": True,
            "why": "exactly at the window; the comparison is inclusive.",
        },
        {
            "name": "valid-at-the-early-edge",
            "body": "ascii",
            "secret": SECRET,
            "header": good,
            "now": base_t - window,
            "valid": True,
            "why": "a receiver whose clock is behind is still inside the window.",
        },
        {
            "name": "stale-just-past-the-window",
            "body": "ascii",
            "secret": SECRET,
            "header": good,
            "now": base_t + window + 1,
            "valid": False,
            "why": "one second past the replay window. A captured request must not verify.",
        },
        {
            "name": "future-just-past-the-window",
            "body": "ascii",
            "secret": SECRET,
            "header": good,
            "now": base_t - window - 1,
            "valid": False,
            "why": "the window is symmetric; a timestamp from the future is refused too.",
        },
        {
            "name": "wrong-secret",
            "body": "ascii",
            "secret": OTHER_SECRET,
            "header": good,
            "now": base_t,
            "valid": False,
            "why": "right body, right window, a signature from a different secret.",
        },
        {
            "name": "empty-secret",
            "body": "ascii",
            "secret": "",
            "header": sign_platform_payload("", raw_bodies["ascii"], timestamp=base_t),
            "now": base_t,
            "valid": False,
            "why": "an unconfigured secret must reject everything, not verify everything.",
        },
        {
            "name": "tampered-body",
            "body": "ascii-tampered",
            "secret": SECRET,
            "header": good,
            "now": base_t,
            "valid": False,
            "why": "one byte of the body changed after signing.",
        },
        {
            "name": "tampered-signature",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t},v1={digest[:-1]}{'0' if digest[-1] != '0' else '1'}",
            "now": base_t,
            "valid": False,
            "why": "one hex character of the digest changed.",
        },
        {
            "name": "uppercase-hex",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t},v1={digest.upper()}",
            "now": base_t,
            "valid": False,
            "why": "the comparison is byte-exact and the server sends lowercase.",
        },
        {
            "name": "signature-of-a-different-timestamp",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t},v1="
            + sign_platform_payload(SECRET, raw_bodies["ascii"], timestamp=base_t + 1).split(
                "v1=", 1
            )[1],
            "now": base_t,
            "valid": False,
            "why": "the timestamp is inside the signed string, so it cannot be moved.",
        },
        {
            "name": "timestamp-in-milliseconds",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t * 1000},v1={digest}",
            "now": base_t,
            "valid": False,
            "why": "`t` is unix seconds. Milliseconds land far outside the window.",
        },
        {
            "name": "non-integer-timestamp",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t=not-a-number,v1={digest}",
            "now": base_t,
            "valid": False,
            "why": "a parse failure is a rejection, never an exception the caller sees.",
        },
        {
            "name": "missing-v1",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t}",
            "now": base_t,
            "valid": False,
            "why": "no digest to compare.",
        },
        {
            "name": "missing-t",
            "body": "ascii",
            "secret": SECRET,
            "header": f"v1={digest}",
            "now": base_t,
            "valid": False,
            "why": "without a timestamp the freshness check cannot run.",
        },
        {
            "name": "empty-header",
            "body": "ascii",
            "secret": SECRET,
            "header": "",
            "now": base_t,
            "valid": False,
            "why": "a missing header is a rejection.",
        },
        {
            "name": "garbage-header",
            "body": "ascii",
            "secret": SECRET,
            "header": "sha256=deadbeef",
            "now": base_t,
            "valid": False,
            "why": "the inbound BTCPay scheme is a different one; it must not be accepted here.",
        },
        {
            "name": "v2-element-beside-v1",
            "body": "ascii",
            "secret": SECRET,
            "header": f"{good},v2=0000000000000000000000000000000000000000000000000000000000000000",
            "now": base_t,
            "valid": True,
            "why": "docs/reference/versioning.md promises a future scheme ships beside `v1=`, "
            "not instead of it. A verifier that refuses unknown elements breaks on the day "
            "`v2=` is added.",
        },
        {
            "name": "v2-only",
            "body": "ascii",
            "secret": SECRET,
            "header": f"t={base_t},v2={digest}",
            "now": base_t,
            "valid": False,
            "why": "a `v1` verifier must not accept a digest labelled as another scheme.",
        },
    ]

    for name in ("unicode", "quotes-and-backslashes", "empty", "large", "binary"):
        cases.append(
            {
                "name": f"valid-{name}-body",
                "body": name,
                "secret": SECRET,
                "header": sign_platform_payload(SECRET, raw_bodies[name], timestamp=base_t),
                "now": base_t,
                "valid": True,
                "why": f"the {name} body verifies as itself.",
            }
        )
    return cases


def build() -> dict[Path, str]:
    rendered = json.dumps(build_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return {VECTORS_PATH: rendered}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; fail if the committed file is not what the code produces",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path, rendered in build().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == rendered:
            continue
        if args.check:
            stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT).as_posix()}")

    if stale:
        for path in stale:
            print(
                f"::error file={path.relative_to(REPO_ROOT).as_posix()}::"
                "out of date; run `python scripts/export_signature_vectors.py` and commit it",
                file=sys.stderr,
            )
        return 1
    if args.check:
        print("the committed signature vectors match the code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
