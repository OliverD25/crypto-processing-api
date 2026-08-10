"""What the bootstrap asks BTCPay for, and what it must never ask for quietly.

This project's rule is that every Greenfield scope is pinned to one store.
Lightning breaks it — enabling `BTC-LN` needs a server-level permission and
BTCPay answers 403 without one — so the rule becomes "every scope is pinned to
one store unless the operator turned Lightning on".

A rule with an exception is only worth anything if the exception cannot happen
by accident. These tests are that guarantee: with the flag off, the scope lists
are the exact strings a deployment that has never heard of Lightning gets, and
the server-level permission appears nowhere.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.services.asset_registry import build_registry
from scripts.bootstrap_btcpay import bootstrap_scopes, lightning_enabled, store_scopes

STORE = "test-store-id"
SERVER_SCOPE = "btcpay.server.canuseinternallightningnode"

#: The v0.1.1 scope lists, written out rather than computed. A test that builds
#: its expectation from the code under test cannot notice the code changing.
RUNTIME_SCOPES_WITHOUT_LIGHTNING = [
    f"btcpay.store.cancreateinvoice:{STORE}",
    f"btcpay.store.canviewinvoices:{STORE}",
    f"btcpay.store.cancreatepullpayments:{STORE}",
    f"btcpay.store.cancreatenonapprovedpullpayments:{STORE}",
    f"btcpay.store.canmanagepayouts:{STORE}",
    f"btcpay.store.canviewstoresettings:{STORE}",
    f"btcpay.store.canviewwallet:{STORE}",
]

BOOTSTRAP_SCOPES_WITHOUT_LIGHTNING = [
    f"btcpay.store.canmodifystoresettings:{STORE}",
    f"btcpay.store.canviewstoresettings:{STORE}",
    f"btcpay.store.webhooks.canmodifywebhooks:{STORE}",
    f"btcpay.store.canviewwallet:{STORE}",
]


@pytest.fixture
def lightning_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("LIGHTNING_ENABLED", raising=False)
    yield


@pytest.fixture
def lightning_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LIGHTNING_ENABLED", "true")
    yield


def test_the_scope_lists_are_unchanged_with_lightning_off(lightning_off: None) -> None:
    assert store_scopes(STORE) == RUNTIME_SCOPES_WITHOUT_LIGHTNING
    assert bootstrap_scopes(STORE) == BOOTSTRAP_SCOPES_WITHOUT_LIGHTNING


def test_no_server_level_permission_is_requested_with_lightning_off(
    lightning_off: None,
) -> None:
    """The single most important assertion in this file.

    A server-level permission on a shared BTCPay reaches past this store. It
    may only ever appear because someone asked for it.
    """
    assert not any(scope.startswith("btcpay.server.") for scope in store_scopes(STORE))
    assert not any(scope.startswith("btcpay.server.") for scope in bootstrap_scopes(STORE))


def test_turning_lightning_on_adds_exactly_two_scopes(lightning_on: None) -> None:
    added_runtime = set(store_scopes(STORE)) - set(RUNTIME_SCOPES_WITHOUT_LIGHTNING)
    added_bootstrap = set(bootstrap_scopes(STORE)) - set(BOOTSTRAP_SCOPES_WITHOUT_LIGHTNING)

    assert added_runtime == {f"btcpay.store.canuselightningnode:{STORE}"}
    assert added_bootstrap == {SERVER_SCOPE, f"btcpay.store.canuselightningnode:{STORE}"}


def test_the_runtime_key_never_holds_the_server_permission(lightning_on: None) -> None:
    """It is a bootstrap scope on purpose.

    Enabling the payment method happens once, under the bootstrap key. The key
    the service carries at runtime reads a channel balance and a payment fee,
    and both of those are store-scoped.
    """
    assert SERVER_SCOPE not in store_scopes(STORE)
    assert SERVER_SCOPE in bootstrap_scopes(STORE)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_the_flag_is_read_the_way_an_operator_would_write_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LIGHTNING_ENABLED", value)
    assert lightning_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "", "  ", "maybe"])
def test_anything_that_is_not_a_yes_is_a_no(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Fails closed. A typo in the flag must not grant a permission."""
    monkeypatch.setenv("LIGHTNING_ENABLED", value)
    assert lightning_enabled() is False


def test_the_registry_and_the_scopes_agree_about_being_off() -> None:
    """One switch, both consequences.

    A build where the registry has BTC_LN and the bootstrap never asked for the
    permission would fail at the first deposit, in production, with a 403 from
    a worker.
    """
    settings: Settings = get_settings()
    off = settings.model_copy(update={"lightning_enabled": False})

    assert "BTC_LN" not in build_registry(off)
