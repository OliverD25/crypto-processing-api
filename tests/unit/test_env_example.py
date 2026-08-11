""".env.example is the configuration reference, so it has to stay true.

The file says at the top that every variable the service reads is listed here.
Nothing enforced that, and it had drifted by ten variables — including
`PLATFORM_WEBHOOK_URL` and `PLATFORM_WEBHOOK_SECRET`, the pair that decides
whether outbound webhooks are signed. An operator reading only this file would
never have learned they exist.

Three properties, all cheap and all easy to break by accident:

- every `Settings` field is documented here,
- nothing is documented here that `Settings` does not read,
- and the file as shipped is a configuration the service would actually start
  with.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from crypto_processing_api.config import Settings
from tests.conftest import REPO_ROOT

ENV_EXAMPLE = REPO_ROOT / ".env.example"

ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

#: Fields deliberately left out of the reference. Empty, and that is the point:
#: adding a name here is a decision someone has to write a reason for, not a
#: silent omission.
INTENTIONALLY_UNDOCUMENTED: frozenset[str] = frozenset()


@pytest.fixture(scope="module")
def documented() -> list[str]:
    return ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))


def test_every_setting_is_documented(documented: list[str]) -> None:
    expected = {name.upper() for name in Settings.model_fields} - INTENTIONALLY_UNDOCUMENTED
    missing = sorted(expected - set(documented))
    assert not missing, (
        f"{len(missing)} Settings field(s) are missing from .env.example: "
        f"{', '.join(missing)}. Add them with a comment saying what they do, "
        "or add them to INTENTIONALLY_UNDOCUMENTED with a reason."
    )


def test_nothing_documented_is_a_setting_the_service_never_reads(documented: list[str]) -> None:
    """A variable nobody reads is worse than an undocumented one: an operator
    sets it, sees no effect, and stops trusting the whole file."""
    fields = {name.upper() for name in Settings.model_fields}
    stale = sorted(set(documented) - fields)
    assert not stale, f".env.example documents variables Settings does not read: {stale}"


def test_no_variable_is_assigned_twice(documented: list[str]) -> None:
    """Copying the file gives whichever value came last, which is a coin toss."""
    counts = Counter(documented)
    duplicated = sorted(name for name, count in counts.items() if count > 1)
    assert not duplicated, f".env.example assigns these more than once: {duplicated}"


def test_the_shipped_defaults_are_a_configuration_that_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file is the thing people copy. If it does not validate, the first
    thing a new deployment sees is a startup crash."""
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)

    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]

    # Spot-check the values the file's own prose promises.
    assert settings.environment == "development"
    assert settings.debug is False
    assert settings.usdt_auto_withdraw is False
    assert settings.lightning_enabled is False
    assert settings.outbound_configured is False
    assert settings.custody_tolerance_units == 0


def test_the_documented_defaults_match_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where .env.example states a number, it must be the number the field
    defaults to — otherwise the reference quietly documents a value nobody
    running without a .env file will ever get."""
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    from_file = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]

    # These are the fields whose commented value in .env.example is presented
    # as "the default", rather than as an example a deployment must replace.
    for field_name in (
        "idempotency_stale_seconds",
        "idempotency_ttl_hours",
        "seed_btc_withdrawal_auto_limit",
        "seed_btc_withdrawal_daily_cap",
        "seed_usdt_withdrawal_flat_fee",
        "webhook_max_attempts",
        "reconcile_invariant_interval_seconds",
        "worker_heartbeat_stale_seconds",
        "webhook_signature_failure_threshold",
        "custody_tolerance_units",
        "outbound_delivery_interval_seconds",
        "outbound_http_timeout_seconds",
        "trx_alert_threshold",
        "tron_confirmations",
        "btc_dust_threshold_sat",
    ):
        default = Settings.model_fields[field_name].get_default(call_default_factory=True)
        assert getattr(from_file, field_name) == default, (
            f".env.example documents {field_name.upper()} as something other than "
            "the code's default"
        )
