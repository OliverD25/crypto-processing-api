"""The drill runner's cold-start readiness gate.

A nightly run mines 101 blocks and then immediately asks for a deposit invoice.
While NBXplorer indexes those blocks BTCPay refuses, the api turns the refusal
into a 502, and drill 1 fails half a second in with something that looks like a
bug in this service. `wait_for_invoice_capability` waits that out.

It is worth a test because both halves of it are dangerous to get wrong. Retry
too little and the nightly stays flaky on slow hardware. Retry too much — any
status, not just the one — and a genuinely broken run spends three minutes
pretending it might recover, then reports the wrong error.

The gate itself needs a whole regtest stack. This tests the decision it makes,
with the clock and the probe handed to it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from scripts.dev.smoke_test import (
    SmokeFailure,
    btcpay_not_ready,
    wait_for_invoice_capability,
)

NOT_READY = (502, '{"detail":"BTCPay rejected the invoice request"}')
CREATED = (201, '{"deposit_id":"019f...","status":"pending"}')


class Clock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def probing(*answers: tuple[int, str]) -> Callable[[], tuple[int, str]]:
    """A probe that returns each answer in turn, then repeats the last one."""
    remaining = list(answers)

    def probe() -> tuple[int, str]:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return probe


def test_a_warm_stack_costs_one_attempt_and_no_waiting() -> None:
    clock = Clock()
    probe = probing(CREATED)

    attempts = wait_for_invoice_capability(
        probe,
        timeout=180,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert attempts == 1
    assert clock.slept == []


def test_the_cold_start_refusal_is_waited_out() -> None:
    clock = Clock()
    probe = probing(NOT_READY, NOT_READY, NOT_READY, CREATED)

    attempts = wait_for_invoice_capability(
        probe,
        timeout=180,
        interval=5,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert attempts == 4
    assert clock.slept == [5, 5, 5]


@pytest.mark.parametrize(
    "answer",
    [
        (401, '{"detail":"invalid API key"}'),
        (404, '{"detail":"unknown asset"}'),
        (422, '{"detail":[{"loc":["body","asset"]}]}'),
        (500, "Internal Server Error"),
        # A 502 with a different reason is still not this one. The gate keys on
        # the message, because the status alone would swallow a proxy's error
        # page and call it "warming up".
        (502, '{"detail":"upstream timed out"}'),
        # 503 is the pooled-address refusal, which is a USDT condition and a
        # different problem with a different fix.
        (503, '{"detail":{"code":"DEPOSIT_TEMPORARILY_UNAVAILABLE"}}'),
    ],
)
def test_every_other_refusal_fails_immediately(answer: tuple[int, str]) -> None:
    clock = Clock()
    probe = probing(answer)

    with pytest.raises(SmokeFailure, match="not the cold-start refusal"):
        wait_for_invoice_capability(
            probe,
            timeout=180,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.slept == [], "a refusal that will never clear must not be waited on"


def test_the_wait_is_bounded_and_says_what_it_last_saw() -> None:
    clock = Clock()
    probe = probing(NOT_READY)

    with pytest.raises(SmokeFailure) as failure:
        wait_for_invoice_capability(
            probe,
            timeout=30,
            interval=10,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert "after 30s" in str(failure.value)
    assert "BTCPay rejected the invoice request" in str(failure.value)
    # 0s, 10s, 20s, 30s: the fourth attempt is the one that finds the deadline.
    assert clock.slept == [10, 10, 10]


def test_a_success_after_the_deadline_still_counts() -> None:
    """The deadline is checked only when an attempt failed, not before one."""
    clock = Clock()
    probe = probing(NOT_READY, CREATED)
    clock.now = 1_000_000.0

    attempts = wait_for_invoice_capability(
        probe,
        timeout=1,
        interval=60,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert attempts == 2


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (502, '{"detail":"BTCPay rejected the invoice request"}', True),
        (502, "BTCPay rejected the invoice request", True),
        (502, '{"detail":"something else"}', False),
        (500, '{"detail":"BTCPay rejected the invoice request"}', False),
        (201, "", False),
    ],
)
def test_the_predicate_reads_status_and_reason_together(
    status_code: int, body: str, expected: bool
) -> None:
    assert btcpay_not_ready(status_code, body) is expected
