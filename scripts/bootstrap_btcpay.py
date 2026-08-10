#!/usr/bin/env python
"""Configure a BTCPay Server instance for this service. Safe to re-run.

    python scripts/bootstrap_btcpay.py

Against the regtest stack it creates, in order: the first admin user, a store,
a BTCPay-generated hot wallet, the settlement speed policy, a webhook pointing
at the api container, and a Greenfield API key holding only the scopes this
service needs. Ids and secrets land in .env.regtest.generated, which is
gitignored.

Every step checks for the object before creating it, so a second run reports
"exists" and changes nothing. The one exception is the webhook secret: BTCPay
never returns it after creation, so if the webhook exists and we have no
recorded secret, a fresh one is written to both sides.

A hot wallet is required, not a watch-only xpub: BTCPay's automated payout
processor has to sign. On regtest that is free; in production it is the reason
the float is the loss ceiling.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / ".env.regtest.generated"

BTC_PAYMENT_METHOD_ID = "BTC-CHAIN"
LN_PAYMENT_METHOD_ID = "BTC-LN"


def lightning_enabled() -> bool:
    """Off unless asked for, and the default is the security decision.

    Enabling BTC-LN through the API needs a **server-level** BTCPay permission.
    Every other scope this script requests is pinned to one store, and that is
    a property worth keeping by default rather than by intention. See
    docs/security.md, threat 5.
    """
    return os.environ.get("LIGHTNING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# 1 confirmation. HighSpeed (0-conf) would credit deposits that a single-block
# reorg can still take away, and regtest is where that habit gets formed.
SPEED_POLICY = "MediumSpeed"

# Seconds after expiry that BTCPay keeps attributing payments to an invoice.
# Set explicitly rather than left at the default, because the deposit sweep
# aligns its polling window to it: past this point a payment to that address
# belongs to no invoice at all, and only the wallet scan can find it.
MONITORING_EXPIRATION_SECONDS = 86400

WEBHOOK_EVENTS = [
    "InvoiceReceivedPayment",
    "InvoiceProcessing",
    "InvoicePaymentSettled",
    "InvoiceSettled",
    "InvoiceExpired",
    "InvoiceInvalid",
    "PayoutCreated",
    "PayoutApproved",
    "PayoutUpdated",
]


# Minimal scopes, all pinned to the one store. Webhook management and
# payout-processor configuration are deliberately absent: those are bootstrap
# operations and run under the admin's Basic auth, not under the key the
# service carries at runtime.
def store_scopes(store_id: str) -> list[str]:
    scopes = [
        f"btcpay.store.cancreateinvoice:{store_id}",
        f"btcpay.store.canviewinvoices:{store_id}",
        # Payout creation is guarded by its own pair of scopes in 2.4.2, and
        # `approved: true` needs the non-approved one: our service decides
        # approval, so BTCPay's AwaitingApproval stage is skipped.
        f"btcpay.store.cancreatepullpayments:{store_id}",
        f"btcpay.store.cancreatenonapprovedpullpayments:{store_id}",
        # Reading, cancelling and marking payouts. The swagger says
        # canmanagepullpayments; the running 2.4.2 answers 403 with
        # missingPermission "btcpay.store.canmanagepayouts". The server wins,
        # and the fact sheet flagged exactly this as unverified.
        f"btcpay.store.canmanagepayouts:{store_id}",
        f"btcpay.store.canviewstoresettings:{store_id}",
        # Read-only wallet access. Needed by the unattributed-receive scan,
        # which is the only thing that finds coins paid to an address BTCPay
        # has stopped watching.
        f"btcpay.store.canviewwallet:{store_id}",
    ]
    if lightning_enabled():
        # Reading the node's channel balance (the custody source for BTC_LN)
        # and the routing fee of a completed payment. Still store-scoped: the
        # server-level permission below is a bootstrap-only scope and the
        # runtime key never holds it.
        scopes.append(f"btcpay.store.canuselightningnode:{store_id}")
    return scopes


# What later runs need in order to re-check and repair the setup. Broader than
# the runtime key, still pinned to the one store, and still never unrestricted.
def bootstrap_scopes(store_id: str) -> list[str]:
    scopes = [
        f"btcpay.store.canmodifystoresettings:{store_id}",
        f"btcpay.store.canviewstoresettings:{store_id}",
        f"btcpay.store.webhooks.canmodifywebhooks:{store_id}",
        f"btcpay.store.canviewwallet:{store_id}",
    ]
    if lightning_enabled():
        # The one server-level permission this project ever requests, and it is
        # requested only because BTCPay requires it to attach the internal
        # Lightning node to a store — a store-scoped key gets a 403. It is held
        # by the bootstrap key, not by the key the service runs on.
        #
        # What it grants: use of the server's internal Lightning node. On a
        # single-tenant box that is the same node this store would use anyway.
        # On a shared BTCPay it is broader than this project's usual rule of
        # "every scope pinned to one store", and docs/security.md says so.
        scopes.append("btcpay.server.canuseinternallightningnode")
        scopes.append(f"btcpay.store.canuselightningnode:{store_id}")
    return scopes


def log(message: str) -> None:
    print(f"[bootstrap] {message}", file=sys.stderr)


class BootstrapError(RuntimeError):
    pass


@dataclass
class Config:
    url: str = field(
        default_factory=lambda: os.environ.get("BTCPAY_BOOTSTRAP_URL", "http://127.0.0.1:14142")
    )
    internal_url: str = field(
        default_factory=lambda: os.environ.get("BTCPAY_INTERNAL_URL", "http://btcpayserver:49392")
    )
    store_name: str = field(
        default_factory=lambda: os.environ.get("BTCPAY_STORE_NAME", "cpapi-regtest")
    )
    webhook_url: str = field(
        default_factory=lambda: os.environ.get(
            "BTCPAY_WEBHOOK_URL", "http://api:8000/webhooks/btcpay"
        )
    )
    admin_email: str = field(
        default_factory=lambda: os.environ.get("BTCPAY_ADMIN_EMAIL", "admin@cpapi.local")
    )
    admin_password: str = field(default_factory=lambda: os.environ.get("BTCPAY_ADMIN_PASSWORD", ""))
    output_path: Path = field(
        default_factory=lambda: Path(os.environ.get("BTCPAY_BOOTSTRAP_OUTPUT", DEFAULT_OUTPUT))
    )
    wait_seconds: int = field(
        default_factory=lambda: int(os.environ.get("BTCPAY_BOOTSTRAP_WAIT", "300"))
    )


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Generated by scripts/bootstrap_btcpay.py — do not commit.",
        "# Re-running the script keeps these values unless the object is gone.",
        "",
    ]
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    # Windows hosts do not honour POSIX modes; the file is gitignored either way.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def generate_password() -> str:
    # ASP.NET Identity wants a digit, both cases and a symbol.
    return f"{secrets.token_urlsafe(24)}aA1!"


class BTCPayAdmin:
    """Greenfield calls, under Basic auth on the first run and an API key after.

    Basic auth is not a durable option. BTCPay only accepts it for an account
    younger than five minutes, with the comment "give new accounts time to
    create API keys via the Greenfield API" — after that the user has to enable
    it by hand in the UI. So the first run has one short window in which to
    mint the key every later run uses.
    """

    def __init__(self, base_url: str, authorization: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": authorization},
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=30.0),
        )

    @classmethod
    def from_basic(cls, base_url: str, email: str, password: str) -> BTCPayAdmin:
        token = base64.b64encode(f"{email}:{password}".encode()).decode()
        return cls(base_url, f"Basic {token}")

    @classmethod
    def from_token(cls, base_url: str, api_key: str) -> BTCPayAdmin:
        return cls(base_url, f"token {api_key}")

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, path, **kwargs)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise BootstrapError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        return response.json() if response.content else None


def wait_for_btcpay(config: Config) -> None:
    deadline = time.monotonic() + config.wait_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{config.url.rstrip('/')}/api/v1/health", timeout=5.0)
            if response.status_code == 200:
                log(f"BTCPay is up: {response.text.strip()}")
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        time.sleep(3)
    raise BootstrapError(
        f"BTCPay at {config.url} did not answer /api/v1/health within "
        f"{config.wait_seconds}s (last: {last_error})"
    )


def ensure_admin_user(config: Config) -> BTCPayAdmin:
    """Create the first admin, or authenticate as an existing new-enough one."""
    admin = BTCPayAdmin.from_basic(config.url, config.admin_email, config.admin_password)
    response = admin.request("GET", "/api/v1/users/me")
    if response.status_code == 200:
        log(f"admin user exists: {config.admin_email}")
        return admin

    created = httpx.post(
        f"{config.url.rstrip('/')}/api/v1/users",
        json={
            "email": config.admin_email,
            "password": config.admin_password,
            "isAdministrator": True,
        },
        timeout=30.0,
    )
    if created.status_code >= 400:
        admin.close()
        raise BootstrapError(
            "could not authenticate or create an admin user "
            f"({created.status_code}: {created.text[:300]}). "
            "BTCPay accepts Basic auth only for an account younger than five minutes, so "
            "this happens when the bootstrap key in .env.regtest.generated is gone or "
            "revoked and the admin account is older than that. "
            "Ways out: enable Greenfield Basic auth for the user in the BTCPay UI "
            "(Account > API Keys), point BTCPAY_ADMIN_EMAIL/BTCPAY_ADMIN_PASSWORD at a "
            "fresh admin, or wipe the dev stack with "
            "`docker compose -f deploy/docker-compose.regtest.yml down -v`."
        )
    log(f"created admin user {config.admin_email}")
    return admin


def connect(config: Config, existing: dict[str, str]) -> tuple[BTCPayAdmin, bool]:
    """Return a Greenfield client and whether it is able to mint API keys.

    Only Basic auth can create API keys (`POST /api/v1/api-keys` accepts Basic
    or an unrestricted key, and an unrestricted key is exactly what this
    project refuses to hold).
    """
    stored = existing.get("BTCPAY_BOOTSTRAP_KEY", "")
    if stored:
        admin = BTCPayAdmin.from_token(config.url, stored)
        if admin.request("GET", "/api/v1/api-keys/current").status_code == 200:
            log("authenticated with the stored bootstrap API key")
            return admin, False
        admin.close()
        log("stored bootstrap key no longer works; trying basic auth")
    return ensure_admin_user(config), True


def ensure_bootstrap_key(admin: BTCPayAdmin, store_id: str, known_key: str) -> str:
    if known_key:
        return known_key
    created = admin.json(
        "POST",
        "/api/v1/api-keys",
        json={"label": "cpapi bootstrap key", "permissions": bootstrap_scopes(store_id)},
    )
    log("issued a bootstrap API key so later runs do not depend on basic auth")
    return str(created["apiKey"])


def ensure_store(admin: BTCPayAdmin, name: str) -> dict[str, Any]:
    stores: list[dict[str, Any]] = admin.json("GET", "/api/v1/stores") or []
    for store in stores:
        if store.get("name") == name:
            log(f"store exists: {name} ({store['id']})")
            return store
    store = admin.json(
        "POST",
        "/api/v1/stores",
        json={
            "name": name,
            "speedPolicy": SPEED_POLICY,
            "monitoringExpiration": MONITORING_EXPIRATION_SECONDS,
        },
    )
    log(f"created store {name} ({store['id']})")
    return store


def ensure_store_policies(admin: BTCPayAdmin, store: dict[str, Any]) -> None:
    """Pin settlement speed and the monitoring window on the store itself."""
    wanted = {
        "speedPolicy": SPEED_POLICY,
        "monitoringExpiration": MONITORING_EXPIRATION_SECONDS,
    }
    if all(store.get(key) == value for key, value in wanted.items()):
        log(
            f"store policies already set (speed {SPEED_POLICY}, "
            f"monitoring {MONITORING_EXPIRATION_SECONDS}s)"
        )
        return
    body = {key: value for key, value in store.items() if key != "id"}
    body.update(wanted)
    response = admin.request("PUT", f"/api/v1/stores/{store['id']}", json=body)
    if response.status_code >= 400:
        raise BootstrapError(
            f"could not set store policies ({response.status_code}: {response.text[:300]})"
        )
    store.update(wanted)
    log(f"store policies set: speed {SPEED_POLICY}, monitoring {MONITORING_EXPIRATION_SECONDS}s")


def ensure_hot_wallet(admin: BTCPayAdmin, store_id: str) -> None:
    existing = admin.request(
        "GET", f"/api/v1/stores/{store_id}/payment-methods/{BTC_PAYMENT_METHOD_ID}/wallet"
    )
    if existing.status_code == 200:
        log(f"hot wallet exists for {BTC_PAYMENT_METHOD_ID}")
        return

    admin.json(
        "POST",
        f"/api/v1/stores/{store_id}/payment-methods/{BTC_PAYMENT_METHOD_ID}/wallet/generate",
        json={
            "label": "cpapi hot wallet",
            "wordList": "English",
            "wordCount": 12,
            "scriptPubKeyType": "Segwit",
            # The automated payout processor has to sign, so BTCPay keeps the
            # seed. The mnemonic in the response is deliberately not written
            # anywhere by this script.
            "savePrivateKeys": True,
        },
    )
    log(f"generated hot wallet for {BTC_PAYMENT_METHOD_ID}")


def ensure_webhook(
    admin: BTCPayAdmin, store_id: str, url: str, known_secret: str
) -> tuple[str, str]:
    webhooks: list[dict[str, Any]] = admin.json("GET", f"/api/v1/stores/{store_id}/webhooks") or []
    body: dict[str, Any] = {
        "url": url,
        "enabled": True,
        # BTCPay's redelivery budget is small; our own worker owns retries. It
        # still costs nothing and covers a brief restart.
        "automaticRedelivery": True,
        "authorizedEvents": {"everything": False, "specificEvents": WEBHOOK_EVENTS},
    }

    for webhook in webhooks:
        if webhook.get("url") != url:
            continue
        if known_secret:
            log(f"webhook exists: {webhook['id']}")
            return str(webhook["id"]), known_secret
        secret = secrets.token_urlsafe(32)
        admin.json("PUT", f"/api/v1/webhooks/{webhook['id']}", json={**body, "secret": secret})
        log(f"webhook {webhook['id']} exists but its secret was unknown; rotated it")
        return str(webhook["id"]), secret

    secret = secrets.token_urlsafe(32)
    created = admin.json(
        "POST", f"/api/v1/stores/{store_id}/webhooks", json={**body, "secret": secret}
    )
    log(f"created webhook {created['id']} -> {url}")
    return str(created["id"]), str(created.get("secret") or secret)


def ensure_api_key(
    admin: BTCPayAdmin, config: Config, store_id: str, known_key: str, *, can_mint: bool
) -> str:
    required = set(store_scopes(store_id))
    if known_key:
        response = httpx.get(
            f"{config.url.rstrip('/')}/api/v1/api-keys/current",
            headers={"Authorization": f"token {known_key}"},
            timeout=15.0,
        )
        if response.status_code == 200:
            granted = set(response.json().get("permissions") or [])
            missing = required - granted
            if not missing:
                log("greenfield API key still valid")
                return known_key
            # Scope sets grow between milestones; a key that predates one is
            # valid but useless for the new call, which would surface as a
            # confusing 403 deep inside a worker.
            log(f"greenfield API key is missing {sorted(missing)}; issuing a new one")
        else:
            log("recorded greenfield API key no longer works; issuing a new one")

    if not can_mint:
        raise BootstrapError(
            "the runtime API key needs re-issuing, but this run authenticated with the "
            "bootstrap key, which cannot create API keys (only Basic auth or an "
            "unrestricted key can, and this project does not hold one). "
            "Enable Greenfield Basic auth for the admin in the BTCPay UI and re-run, or "
            "wipe the dev stack with "
            "`docker compose -f deploy/docker-compose.regtest.yml down -v`."
        )
    created = admin.json(
        "POST",
        "/api/v1/api-keys",
        json={"label": "cpapi service key", "permissions": store_scopes(store_id)},
    )
    log(f"issued greenfield API key with {len(store_scopes(store_id))} store-scoped permissions")
    return str(created["apiKey"])


def configure_payout_processor(admin: BTCPayAdmin, store_id: str) -> None:
    """Configure the automated on-chain sender.

    This is the component that actually broadcasts. It requires a hot wallet,
    which is why the wallet is generated with savePrivateKeys.

    The design called for a 5-second interval on regtest. BTCPay 2.4.2 refuses
    it: "The minimum interval is 60 seconds". That does not slow the drills
    down, because `processNewPayoutsInstantly` makes a freshly created payout
    go out immediately; the interval only governs the sweep that catches
    anything the instant path missed.

    Production leaves it at ten minutes so the processor can batch several
    payouts into one transaction and pay one miner fee instead of several.
    """
    interval = max(int(os.environ.get("BTCPAY_PAYOUT_INTERVAL_SECONDS", "60")), 60)
    body = {
        "intervalSeconds": interval,
        "feeTargetBlock": int(os.environ.get("BTCPAY_FEE_TARGET_BLOCK", "3")),
        "threshold": "0",
        "processNewPayoutsInstantly": True,
    }
    response = admin.request(
        "PUT",
        f"/api/v1/stores/{store_id}/payout-processors/"
        f"OnChainAutomatedPayoutSenderFactory/{BTC_PAYMENT_METHOD_ID}",
        json=body,
    )
    if response.status_code >= 400:
        raise BootstrapError(
            "could not configure the on-chain payout processor "
            f"({response.status_code}: {response.text[:300]}). Without it approved payouts "
            "are created but never broadcast."
        )
    log(f"payout processor configured: every {interval}s, fee target 3 blocks")


def ensure_lightning_payment_method(admin: BTCPayAdmin, store_id: str) -> None:
    """Point the store at the server's internal Lightning node.

    This is the call that needs `btcpay.server.canuseinternallightningnode`; a
    store-scoped key gets a 403 with that permission named, which is how the
    spike discovered it was required at all.
    """
    existing = admin.request(
        "GET", f"/api/v1/stores/{store_id}/payment-methods/{LN_PAYMENT_METHOD_ID}"
    )
    if existing.status_code == 200 and (existing.json() or {}).get("enabled"):
        log(f"lightning payment method already enabled ({LN_PAYMENT_METHOD_ID})")
        return

    response = admin.request(
        "PUT",
        f"/api/v1/stores/{store_id}/payment-methods/{LN_PAYMENT_METHOD_ID}",
        json={"enabled": True, "config": {"connectionString": "Internal Node"}},
    )
    if response.status_code >= 400:
        raise BootstrapError(
            f"could not enable {LN_PAYMENT_METHOD_ID} on the store "
            f"({response.status_code}: {response.text[:300]}). A 403 here means the "
            "bootstrap key predates LIGHTNING_ENABLED and lacks "
            "btcpay.server.canuseinternallightningnode; delete BTCPAY_BOOTSTRAP_KEY from "
            ".env.regtest.generated and re-run so a new one is issued. Anything else "
            "usually means BTCPAY_BTCLIGHTNING is not set on the BTCPay container."
        )
    log(f"enabled {LN_PAYMENT_METHOD_ID} on the store, using the internal node")


def configure_lightning_payout_processor(admin: BTCPayAdmin, store_id: str) -> None:
    """Configure the automated Lightning sender.

    Without it, approved Lightning payouts are created and never paid.

    One thing to know about this configuration, found by the R4 spike:
    `cancelPayoutAfterFailures` is accepted and then silently dropped from the
    echoed config. BTCPay will **not** cancel a payout it cannot route, however
    many times it fails — it retries for as long as the invoice lives. That is
    why the service has its own deadline (`LN_PAYOUT_TIMEOUT_SECONDS`) rather
    than relying on this processor to give up.
    """
    interval = max(int(os.environ.get("BTCPAY_LN_PAYOUT_INTERVAL_SECONDS", "60")), 60)
    response = admin.request(
        "PUT",
        f"/api/v1/stores/{store_id}/payout-processors/"
        f"LightningAutomatedPayoutSenderFactory/{LN_PAYMENT_METHOD_ID}",
        json={
            "intervalSeconds": interval,
            "processNewPayoutsInstantly": True,
        },
    )
    if response.status_code >= 400:
        raise BootstrapError(
            "could not configure the Lightning payout processor "
            f"({response.status_code}: {response.text[:300]}). Without it approved "
            "Lightning payouts are created but never paid."
        )
    log(f"lightning payout processor configured: every {interval}s, new payouts instantly")


def main() -> int:
    config = Config()
    existing = read_env_file(config.output_path)

    if not config.admin_password:
        config.admin_password = existing.get("BTCPAY_ADMIN_PASSWORD") or generate_password()
    if "BTCPAY_ADMIN_EMAIL" in existing and not os.environ.get("BTCPAY_ADMIN_EMAIL"):
        config.admin_email = existing["BTCPAY_ADMIN_EMAIL"]

    wait_for_btcpay(config)
    admin, can_mint = connect(config, existing)
    try:
        store = ensure_store(admin, config.store_name)
        ensure_store_policies(admin, store)
        store_id = str(store["id"])
        bootstrap_key = existing.get("BTCPAY_BOOTSTRAP_KEY", "")
        if can_mint:
            bootstrap_key = ensure_bootstrap_key(admin, store_id, bootstrap_key)
        ensure_hot_wallet(admin, store_id)
        webhook_id, webhook_secret = ensure_webhook(
            admin, store_id, config.webhook_url, existing.get("BTCPAY_WEBHOOK_SECRET", "")
        )
        api_key = ensure_api_key(
            admin, config, store_id, existing.get("BTCPAY_API_KEY", ""), can_mint=can_mint
        )
        configure_payout_processor(admin, store_id)
        if lightning_enabled():
            ensure_lightning_payment_method(admin, store_id)
            configure_lightning_payout_processor(admin, store_id)
    finally:
        admin.close()

    values = {
        "BTCPAY_URL": config.internal_url,
        "BTCPAY_PUBLIC_URL": config.url,
        "BTCPAY_STORE_ID": store_id,
        "BTCPAY_API_KEY": api_key,
        "BTCPAY_BOOTSTRAP_KEY": bootstrap_key,
        "BTCPAY_WEBHOOK_ID": webhook_id,
        "BTCPAY_WEBHOOK_SECRET": webhook_secret,
        "BTCPAY_ADMIN_EMAIL": config.admin_email,
        "BTCPAY_ADMIN_PASSWORD": config.admin_password,
        "SEED_BTC_PAYMENT_METHOD": BTC_PAYMENT_METHOD_ID,
        "DEPOSIT_MONITORING_MINUTES": str(MONITORING_EXPIRATION_SECONDS // 60),
    }
    if lightning_enabled():
        values["LIGHTNING_ENABLED"] = "true"
        values["SEED_LN_PAYMENT_METHOD"] = LN_PAYMENT_METHOD_ID
    write_env_file(config.output_path, values)
    log(f"wrote {config.output_path}")
    if lightning_enabled():
        log("lightning is on: the key holds one server-level scope, see docs/security.md")
    log("store_id and webhook are ready; deposits are M2")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error
