"""Test-wide environment.

DATABASE_URL is pinned before anything imports the settings, so no test can
accidentally talk to a developer's real database.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://cpapi:cpapi@127.0.0.1:54329/cpapi_test"
)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# Matches FakeBTCPay's defaults. The webhook secret has to be real config
# rather than a test constant, because the endpoint reads it from Settings and
# an unconfigured secret must reject everything.
FAKE_STORE_ID = "store-test"
FAKE_WEBHOOK_SECRET = "test-webhook-secret"

os.environ.setdefault("BTCPAY_URL", "http://btcpay.invalid")
os.environ.setdefault("BTCPAY_API_KEY", "test-api-key")
os.environ.setdefault("BTCPAY_STORE_ID", FAKE_STORE_ID)
os.environ.setdefault("BTCPAY_WEBHOOK_SECRET", FAKE_WEBHOOK_SECRET)

# The suite models a regtest deployment, so withdrawal destinations are regtest
# addresses. MEMPOOL_SPACE_URL is cleared because no test may depend on a third
# party being reachable.
os.environ.setdefault("BITCOIN_NETWORK", "regtest")
os.environ.setdefault("MEMPOOL_SPACE_URL", "")

# --------------------------------------------------------------------------
# Hypothesis profiles
#
# `ci` is derandomized on purpose: a pull request must not go red on someone
# else's luck, and a property failure that only one run in twenty sees teaches
# people to hit rerun. `nightly` takes the random seed and twenty times the
# examples, where an extra ten minutes costs nothing and a real find is worth
# the triage.
# --------------------------------------------------------------------------
from hypothesis import HealthCheck, settings  # noqa: E402

settings.register_profile(
    "ci",
    max_examples=15,
    stateful_step_count=30,
    deadline=None,  # database latency jitter must not fail a test
    derandomize=True,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.register_profile(
    "nightly",
    max_examples=300,
    stateful_step_count=50,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
