#!/usr/bin/env python
"""Assert the BTCPay endpoints and fields this service depends on still exist.

    python scripts/check_btcpay_compat.py

Every one of the assertions below was verified against a running BTCPay 2.4.2
during the build, and several of them contradict what the design documents
assumed — the per-invoice routes moved out of the store scope, payout creation
takes `payoutMethodId` rather than `paymentMethod`, and the payout management
scope is not the one the swagger advertises.

That history is the argument for this script. These are version-dependent
details in a dependency that ships every few weeks, and the failure mode of
drift is not a crash: it is a deposit that never credits, or a confirmed
withdrawal with no transaction id.

Run it in CI against the pinned tag. Bump BTCPAY_TAG only together with the
image tag in the compose files, and expect to fix something.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx

BTCPAY_TAG = "v2.4.2"
SWAGGER_BASE = (
    f"https://raw.githubusercontent.com/btcpayserver/btcpayserver/{BTCPAY_TAG}"
    "/BTCPayServer/wwwroot/swagger/v1"
)
TIMEOUT = 30.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def fetch(document: str) -> dict[str, Any]:
    response = httpx.get(f"{SWAGGER_BASE}/swagger.template.{document}.json", timeout=TIMEOUT)
    response.raise_for_status()
    return dict(response.json())


def has_path(spec: dict[str, Any], path: str, method: str) -> bool:
    return method.lower() in (spec.get("paths", {}).get(path) or {})


def schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(spec.get("components", {}).get("schemas", {}).get(name) or {})


def properties(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Read properties through an allOf, following $ref inside the document.

    InvoiceData carries `metadata` only by inheriting InvoiceDataBase, so a
    reader that does not resolve the reference reports a false failure — and a
    compatibility check that cries wolf gets ignored, which is worse than not
    having one.
    """
    found: dict[str, Any] = dict(node.get("properties") or {})
    for part in node.get("allOf") or []:
        reference = part.get("$ref")
        if reference:
            found.update(properties(spec, schema(spec, reference.rsplit("/", 1)[-1])))
        found.update(part.get("properties") or {})
    return found


def run_checks() -> list[Check]:
    checks: list[Check] = []

    invoices = fetch("invoices")
    checks.append(
        Check(
            "invoice creation is store-scoped",
            has_path(invoices, "/api/v1/stores/{storeId}/invoices", "post"),
        )
    )
    # 2.4.2 moved these out of the store scope; the design documents predate it.
    checks.append(
        Check(
            "per-invoice route is NOT store-scoped",
            has_path(invoices, "/api/v1/invoices/{invoiceId}", "get"),
        )
    )
    checks.append(
        Check(
            "invoice payment-methods route is NOT store-scoped",
            has_path(invoices, "/api/v1/invoices/{invoiceId}/payment-methods", "get"),
        )
    )
    invoice_data = properties(invoices, schema(invoices, "InvoiceData"))
    for field in ("status", "additionalStatus", "metadata", "monitoringExpiration"):
        checks.append(Check(f"InvoiceData.{field}", field in invoice_data))
    payment = properties(invoices, schema(invoices, "Payment"))
    for field in ("id", "value", "status"):
        checks.append(Check(f"Payment.{field}", field in payment))
    checkout = properties(invoices, schema(invoices, "CheckoutOptions"))
    for field in ("expirationMinutes", "monitoringMinutes", "paymentMethods"):
        checks.append(Check(f"CheckoutOptions.{field}", field in checkout))

    payouts = fetch("pull-payments")
    checks.append(
        Check(
            "store payout creation",
            has_path(payouts, "/api/v1/stores/{storeId}/payouts", "post"),
        )
    )
    checks.append(
        Check("payout fetch by id", has_path(payouts, "/api/v1/payouts/{payoutId}", "get"))
    )
    create_payout = properties(payouts, schema(payouts, "CreatePayoutThroughStoreRequest"))
    # The correlation key. Verified live: BTCPay echoes it on GET and in the
    # store payout list, which is what makes stuck-submission resolution
    # unambiguous.
    checks.append(Check("CreatePayoutThroughStoreRequest.metadata", "metadata" in create_payout))
    checks.append(Check("CreatePayoutThroughStoreRequest.approved", "approved" in create_payout))
    base_payout = properties(payouts, schema(payouts, "CreatePayoutRequest"))
    checks.append(
        Check(
            "payout creation takes payoutMethodId (not paymentMethod)",
            "payoutMethodId" in base_payout and "paymentMethod" not in base_payout,
        )
    )
    payout_data = properties(payouts, schema(payouts, "PayoutData"))
    for field in ("state", "metadata", "paymentProof", "payoutMethodId"):
        checks.append(Check(f"PayoutData.{field}", field in payout_data))

    webhooks = fetch("webhooks")
    checks.append(
        Check(
            "webhook creation is store-scoped",
            has_path(webhooks, "/api/v1/stores/{storeId}/webhooks", "post"),
        )
    )
    checks.append(
        Check(
            "delivery redeliver route",
            has_path(
                webhooks,
                "/api/v1/webhooks/{webhookId}/deliveries/{deliveryId}/redeliver",
                "post",
            ),
        )
    )

    wallet = fetch("stores-wallet.on-chain")
    for path in (
        "/api/v1/stores/{storeId}/payment-methods/{paymentMethodId}/wallet",
        "/api/v1/stores/{storeId}/payment-methods/{paymentMethodId}/wallet/transactions",
        "/api/v1/stores/{storeId}/payment-methods/{paymentMethodId}/wallet/feerate",
        "/api/v1/stores/{storeId}/payment-methods/{paymentMethodId}/wallet/generate",
    ):
        checks.append(
            Check(
                f"wallet route {path.rsplit('/', 1)[-1]}",
                has_path(wallet, path, "get" if "generate" not in path else "post"),
            )
        )

    methods = fetch("stores-payment-methods")
    checks.append(
        Check(
            "store payment-methods discovery",
            has_path(methods, "/api/v1/stores/{storeId}/payment-methods", "get"),
        )
    )

    processors = fetch("payout-processors")
    checks.append(
        Check(
            "on-chain payout processor config",
            has_path(
                processors,
                "/api/v1/stores/{storeId}/payout-processors/"
                "OnChainAutomatedPayoutSenderFactory/{paymentMethodId}",
                "put",
            ),
        )
    )

    return checks


def main() -> int:
    try:
        checks = run_checks()
    except httpx.HTTPError as exc:
        print(f"could not fetch the {BTCPAY_TAG} swagger: {exc}", file=sys.stderr)
        # A network failure is not compatibility drift; do not fail the build.
        return 0

    failures = [check for check in checks if not check.ok]
    for check in checks:
        print(f"{'ok  ' if check.ok else 'FAIL'}  {check.name}")

    print(f"\n{len(checks) - len(failures)}/{len(checks)} checks passed against {BTCPAY_TAG}")
    if failures:
        print(
            "\nBTCPay has drifted from what this service assumes. Every failure above "
            "is a place where a deposit may silently stop crediting or a withdrawal "
            "may confirm with no transaction id. Fix the client before bumping the "
            "pinned image.",
            file=sys.stderr,
        )
        print(json.dumps([check.name for check in failures], indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
