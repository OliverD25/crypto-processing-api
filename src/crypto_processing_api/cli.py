"""Operator command line.

    python -m crypto_processing_api.cli migrate
    python -m crypto_processing_api.cli create-api-key --name platform --scope readwrite
    python -m crypto_processing_api.cli revoke-api-key --key-id aB3xQ9zK

Everything except the generated key itself goes to stderr, so the key can be
piped straight into a password manager or clipboard without picking up prose.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core import auth
from crypto_processing_api.db import session_scope
from crypto_processing_api.ledger.service import AssetSpec, seed_assets

DEFAULT_ALEMBIC_INI = "alembic.ini"


def asset_specs(settings: Settings) -> list[AssetSpec]:
    """Seed values for the two MVP assets.

    Read once, at `migrate`. Afterwards the `assets` rows are the only source
    of truth for limits and fees — two places describing the same number is
    how a limit ends up silently disagreeing with itself.
    """
    return [
        AssetSpec(
            id="BTC",
            display_name="Bitcoin",
            decimals=8,
            unit_name="sat",
            btcpay_payment_method=settings.seed_btc_payment_method,
            withdrawal_auto_limit=settings.seed_btc_withdrawal_auto_limit,
            withdrawal_daily_cap=settings.seed_btc_withdrawal_daily_cap,
            withdrawal_user_daily_cap=settings.seed_btc_withdrawal_user_daily_cap,
            withdrawal_min=settings.seed_btc_withdrawal_min,
            withdrawal_flat_fee=settings.seed_btc_withdrawal_flat_fee,
            invoice_currency="BTC",
        ),
        AssetSpec(
            id="USDT_TRC20",
            display_name="Tether USD (TRC-20)",
            decimals=6,
            unit_name="microUSDT",
            btcpay_payment_method=settings.seed_usdt_payment_method,
            withdrawal_auto_limit=settings.seed_usdt_withdrawal_auto_limit,
            withdrawal_daily_cap=settings.seed_usdt_withdrawal_daily_cap,
            withdrawal_user_daily_cap=settings.seed_usdt_withdrawal_user_daily_cap,
            withdrawal_min=settings.seed_usdt_withdrawal_min,
            withdrawal_flat_fee=settings.seed_usdt_withdrawal_flat_fee,
            # Addresses come from the USDt plugin's shared pool, which is what
            # makes attribution heuristic and the review queue load-bearing.
            pooled_addresses=True,
            invoice_currency="USDT",
        ),
    ]


def _alembic_config(settings: Settings, ini_path: str | None) -> Config:
    path = Path(ini_path or os.environ.get("ALEMBIC_CONFIG") or DEFAULT_ALEMBIC_INI).resolve()
    if not path.is_file():
        raise SystemExit(
            f"alembic config not found at {path}. Run from the repository root, "
            f"pass --config, or set ALEMBIC_CONFIG."
        )
    config = Config(str(path))
    config.set_main_option("script_location", str(path.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
    return config


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = get_settings()
    command.upgrade(_alembic_config(settings, args.config), args.revision)
    print(f"schema at {args.revision}", file=sys.stderr)

    if args.no_seed:
        return 0
    with session_scope() as session:
        inserted = seed_assets(session, asset_specs(settings))
    if inserted:
        print(f"seeded assets: {', '.join(inserted)}", file=sys.stderr)
    else:
        print("assets already seeded; database values left untouched", file=sys.stderr)
    return 0


def cmd_create_api_key(args: argparse.Namespace) -> int:
    settings = get_settings()
    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    )
    with session_scope() as session:
        generated, _ = auth.create_api_key(
            session,
            name=args.name,
            scope=args.scope,
            prefix=settings.api_key_prefix,
            expires_at=expires_at,
        )
        key = generated.key
        key_id = generated.key_id

    print(key)
    print(
        f"\nkey_id  {key_id}\nname    {args.name}\nscope   {args.scope}\n"
        f"expires {expires_at.isoformat() if expires_at else 'never'}\n"
        "\nThis is the only time the key is shown. Store it now; only its "
        "SHA-256 hash is in the database.",
        file=sys.stderr,
    )
    return 0


def cmd_revoke_api_key(args: argparse.Namespace) -> int:
    with session_scope() as session:
        revoked = auth.revoke_api_key(session, args.key_id)
    if revoked:
        print(f"revoked {args.key_id}", file=sys.stderr)
        return 0
    print(f"no active key with key_id {args.key_id}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-processing-api",
        description="Operator commands for crypto-processing-api",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="apply migrations and seed the asset rows")
    migrate.add_argument("--revision", default="head", help="target revision (default: head)")
    migrate.add_argument("--config", default=None, help="path to alembic.ini")
    migrate.add_argument(
        "--no-seed", action="store_true", help="skip the idempotent asset seed step"
    )
    migrate.set_defaults(func=cmd_migrate)

    create = subparsers.add_parser("create-api-key", help="mint an API key (printed once)")
    create.add_argument("--name", required=True, help="who this key is for")
    create.add_argument("--scope", required=True, choices=list(auth.SCOPES))
    create.add_argument("--expires-days", type=int, default=None, help="optional expiry in days")
    create.set_defaults(func=cmd_create_api_key)

    revoke = subparsers.add_parser("revoke-api-key", help="revoke a key by its key_id")
    revoke.add_argument("--key-id", required=True)
    revoke.set_defaults(func=cmd_revoke_api_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
