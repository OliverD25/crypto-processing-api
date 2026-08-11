"""A worked platform integration with crypto-processing-api, in one file.

Read this top to bottom. It is the platform side of the contract — the code
*you* write — and it does the whole loop against a real regtest stack: a user
signs in, deposits BTC, watches it credit, and withdraws some of it again.

**A note about the comments.** Everywhere else in this repository a comment
explains only a non-obvious *why*, and never restates what the code does. This
file breaks that rule on purpose. It is teaching material, and the numbered
sections below are the lesson rather than an aside to it.

**What this is not.** It is a demonstration, not a starting template. There is
no real authentication, no database, no CSRF protection and no error page worth
showing a customer. Each of those has a comment where it would go.

The one idea to take away, if you take away nothing else:

    The processing API is the source of truth for balances. This file stores
    none. Every number on every page was read from the API a moment before it
    was rendered.

README.md next to this file has the four commands that start it on the regtest
stack, and a table of what to click and what each step proves.
"""

from __future__ import annotations

import os
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Final

from fastapi import BackgroundTasks, Cookie, FastAPI, Form, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from crypto_processing_client import (
    APIError,
    CryptoProcessingClient,
    CryptoProcessingError,
    DepositResponse,
    PlatformEvent,
    UnknownEventTypeError,
    WebhookVerificationError,
    WithdrawalCreatedResponse,
    WithdrawalResponse,
    parse_event,
)

# ---------------------------------------------------------------------------
# 1. Configuration
#
# Three values, and two of them are secrets. The API key authenticates every
# call this platform makes; the webhook secret is what proves a delivery came
# from the processing service and not from someone who guessed the URL. In
# production both come out of your secret store. On the regtest stack the API
# key is minted by a one-shot container and left in a file, which is why
# `_setting` also accepts `NAME_FILE`.
# ---------------------------------------------------------------------------

HERE: Final = Path(__file__).resolve().parent


def _setting(name: str, default: str = "") -> str:
    """Read `NAME`, or the contents of the file named by `NAME_FILE`."""
    path = os.environ.get(f"{name}_FILE", "")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


API_URL: Final = _setting("CPAPI_BASE_URL", "http://api:8000")
API_KEY: Final = _setting("CPAPI_API_KEY")
WEBHOOK_SECRET: Final = _setting("PLATFORM_WEBHOOK_SECRET")

#: This demo's entire user table. A real platform has its own users, its own
#: passwords and its own sessions; the processing API never sees any of them.
#: It knows a user only as an opaque `external_user_id` string.
DEMO_USERS: Final = ("alice", "bob")

#: Statuses that will not change again on their own. The UI stops polling when
#: it reaches one of these, and so should yours.
DEPOSIT_SETTLED: Final = frozenset({"settled", "expired", "dismissed", "failed"})
WITHDRAWAL_SETTLED: Final = frozenset({"confirmed", "refunded", "rejected"})


# ---------------------------------------------------------------------------
# 2. What a platform stores, and what it must not
#
# This dict is the whole "database". Note what is *not* in it: no balances, no
# deposit amounts, no withdrawal statuses. Those live in the processing API and
# are read on every render. Mirroring them here is how a platform ends up with
# two numbers that disagree and no way to say which is right.
#
# What does belong here is everything the processing API cannot know: which
# events this platform has already handled, and whatever your own product needs
# to show. In a real platform this is your database and the dedup set is a
# unique index on the event id.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """One line in the activity feed."""

    text: str
    #: `webhook` if a delivery caused it, `you` if a click did. The demo shows
    #: this so you can see the webhook path working, or not working.
    source: str


@dataclass
class Platform:
    handled_events: set[str] = field(default_factory=set)
    activity: dict[str, deque[Notice]] = field(default_factory=dict)

    def remember_event(self, event_id: str) -> bool:
        """True the first time this event id is seen, False every time after.

        The same `evt_` id can arrive twice — a delivery that timed out on our
        side was still retried. Without this check a retried
        `withdrawal.completed` would post a second entry in the feed, and in a
        real platform a second credit.
        """
        if event_id in self.handled_events:
            return False
        self.handled_events.add(event_id)
        return True

    def note(self, user: str, text: str, *, source: str) -> None:
        self.activity.setdefault(user, deque(maxlen=25)).appendleft(Notice(text, source))

    def feed(self, user: str) -> Iterable[Notice]:
        return self.activity.get(user, ())


STORE: Final = Platform()


# ---------------------------------------------------------------------------
# 3. One client for the whole process
#
# The client holds an HTTP connection pool, so it is built once and closed at
# shutdown rather than built per request. It mints an `Idempotency-Key` for
# every mutating call and reuses it across that call's own retries, which is
# the rule the integration guide puts in bold: a retry with a *new* key is a
# second withdrawal, not a retry.
#
# Building it raises if the key is missing. That is deliberate — there is no
# unauthenticated mode to fall back to, and a demo that starts anyway would
# fail later with something less obvious.
# ---------------------------------------------------------------------------

CPA: Final = CryptoProcessingClient(API_URL, api_key=API_KEY)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    CPA.close()


app = FastAPI(title="platform-demo", lifespan=_lifespan)
templates = Jinja2Templates(directory=HERE / "templates")


def _page(request: Request, name: str, **context: object) -> Response:
    return templates.TemplateResponse(request, name, context)


# ---------------------------------------------------------------------------
# 4. "Log in"
#
# Pick a name from a dropdown and it goes in a cookie. That is the entire
# authentication story here, and it is a lie: your platform already owns
# accounts, passwords, sessions and whatever second factor you require, and
# none of it is any of the processing API's business.
#
# What the API does need is a stable `external_user_id`. It is opaque — use
# your own primary key. Two rules: it must not change under a user, because
# their balance is keyed on it, and it must not be reused for a different user
# after an account is deleted.
# ---------------------------------------------------------------------------


@app.get("/")
def home(request: Request, user: Annotated[str | None, Cookie()] = None) -> Response:
    if user not in DEMO_USERS:
        return _page(request, "login.html", users=DEMO_USERS)
    return _page(request, "dashboard.html", user=user, request_id=str(uuid.uuid4()))


@app.post("/login")
def login(user: Annotated[str, Form()]) -> Response:
    response = RedirectResponse("/", status_code=303)
    if user in DEMO_USERS:
        # A real session cookie is signed, short-lived and rotated on login.
        response.set_cookie("user", user, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("user")
    return response


# ---------------------------------------------------------------------------
# 5. Deposits
#
# `create_deposit` returns an address and a checkout link. Show either: the
# address for a wallet the user pastes into, the link for BTCPay's own checkout
# page with its QR code and payment tracking.
#
# The address is single use — every deposit gets a fresh one — and it keeps
# receiving coins after the deposit expires. Expiry is when we stop treating a
# payment as ordinary, not when the address goes dead. So never cache one for
# reuse, and never tell a user that late money is lost: it lands in an
# operator's review queue instead.
#
# `expected_amount` is an integer count of smallest units as a string —
# satoshis for BTC. Display-only for BTC, load-bearing for USDT, whose deposit
# addresses come from a shared pool where the amount is what tells one user's
# payment from another's.
# ---------------------------------------------------------------------------


@app.post("/deposits")
def start_deposit(
    request: Request,
    amount_sats: Annotated[str, Form()],
    user: Annotated[str | None, Cookie()] = None,
) -> Response:
    if user not in DEMO_USERS:
        return _page(request, "_problem.html", message="Sign in first.")
    try:
        deposit = CPA.create_deposit(
            external_user_id=user,
            asset="BTC",
            expected_amount=amount_sats.strip() or None,
        )
    except APIError as error:
        return _refused(request, error)
    STORE.note(user, f"asked for a deposit address for {amount_sats} sat", source="you")
    return _deposit_card(request, deposit)


@app.get("/deposits/{deposit_id}")
def deposit_card(request: Request, deposit_id: str) -> Response:
    """The fragment HTMX re-fetches while the deposit is still moving.

    Polling is not a fallback for webhooks that failed. It is the thing that is
    always correct; webhooks only make it faster.
    """
    return _deposit_card(request, CPA.get_deposit(deposit_id))


def _deposit_card(request: Request, deposit: DepositResponse) -> Response:
    return _page(
        request,
        "_deposit.html",
        d=deposit,
        # `review` is not an error. It means the money is in custody and an
        # operator has to confirm who it belongs to. Show "being verified",
        # keep polling, and it becomes `settled`.
        moving=deposit.status not in DEPOSIT_SETTLED,
    )


# ---------------------------------------------------------------------------
# 6. Balances
#
# Read on every render, never stored. `available` is what a withdrawal can
# spend; `held` is reserved by a withdrawal already in flight. They are two
# separate accounts in the service's ledger rather than one number and a
# subtraction, which is why they cannot drift apart.
#
# Amounts arrive as decimal strings — `"0.50000000"`, not `0.5`. Keep them as
# strings, or parse them with a decimal type. Turning one into a float is how a
# satoshi goes missing, and 21 million BTC in satoshis is already past
# JavaScript's safe integer range.
# ---------------------------------------------------------------------------


@app.get("/balances")
def balances(request: Request, user: Annotated[str | None, Cookie()] = None) -> Response:
    if user not in DEMO_USERS:
        return _page(request, "_problem.html", message="Sign in first.")
    return _page(request, "_balances.html", balances=CPA.get_user_balances(user).balances)


@app.get("/activity")
def activity(request: Request, user: Annotated[str | None, Cookie()] = None) -> Response:
    if user not in DEMO_USERS:
        return _page(request, "_problem.html", message="Sign in first.")
    return _page(request, "_activity.html", notices=STORE.feed(user))


# ---------------------------------------------------------------------------
# 7. Withdrawals
#
# `amount` is the **gross** amount in integer smallest units, as a string: what
# leaves the user's balance. The fee comes out of it, so the destination
# receives less.
#
# Note the `idempotency_key`. The client would mint one for you, and for a
# single call that is right. Here the form carries an id generated when the
# page was rendered, so submitting the same form twice — a double click, a
# refresh, a flaky connection — is one withdrawal and not two. That is the
# ergonomic worth copying: derive the key from the *operation* your system
# already has an id for, not from the attempt.
#
# `pending_approval` is not a rejection: the hold is placed before the call
# returns, so the money is already reserved and an operator has to look at it.
# Only a 4xx means nothing was reserved. `failed` does not mean the money is
# back either — a payout that looks failed can still confirm, so the hold stays
# until an operator states in writing that it will not.
# ---------------------------------------------------------------------------


@app.post("/withdrawals")
def start_withdrawal(
    request: Request,
    amount: Annotated[str, Form()],
    destination: Annotated[str, Form()],
    request_id: Annotated[str, Form()],
    user: Annotated[str | None, Cookie()] = None,
) -> Response:
    if user not in DEMO_USERS:
        return _page(request, "_problem.html", message="Sign in first.")
    try:
        created = CPA.create_withdrawal(
            external_user_id=user,
            asset="BTC",
            amount=amount.strip(),
            destination_address=destination.strip(),
            idempotency_key=request_id,
        )
    except APIError as error:
        # Everything the service refuses, it refuses with a reason: not enough
        # available balance, an address that fails its checksum, an amount
        # below the minimum or whose net would be dust. Show it; the user can
        # act on all of them.
        return _refused(request, error)
    STORE.note(user, f"requested a withdrawal of {amount} sat", source="you")
    return _withdrawal_card(request, created)


@app.get("/withdrawals/{withdrawal_id}")
def withdrawal_card(request: Request, withdrawal_id: str) -> Response:
    return _withdrawal_card(request, CPA.get_withdrawal(withdrawal_id))


def _withdrawal_card(
    request: Request, withdrawal: WithdrawalCreatedResponse | WithdrawalResponse
) -> Response:
    # Only the creation response carries `approval_reason` — which of the three
    # gates sent this one to an operator. Worth showing: the same amount can be
    # auto-approved in the morning and queued in the afternoon, because the
    # per-asset 24-hour cap is shared across every user, not per user.
    reason: str | None = None
    if isinstance(withdrawal, WithdrawalCreatedResponse) and isinstance(
        withdrawal.approval_reason, str
    ):
        reason = withdrawal.approval_reason
    return _page(
        request,
        "_withdrawal.html",
        w=withdrawal,
        reason=reason,
        moving=withdrawal.status not in WITHDRAWAL_SETTLED,
    )


def _refused(request: Request, error: APIError) -> Response:
    return _page(request, "_problem.html", message=str(error))


@app.exception_handler(CryptoProcessingError)
def _client_failed(request: Request, error: Exception) -> Response:
    """The backstop for the read paths: the API answered badly, or not at all.

    `CryptoProcessingError` is the base of everything the client raises,
    including the transport failure you get when the service is restarting.
    Reads have no `try` of their own because there is nothing route-specific to
    say about them — the fragment shows the reason and the next poll, three
    seconds later, tries again.
    """
    return _page(request, "_problem.html", message=str(error))


# ---------------------------------------------------------------------------
# 8. The webhook endpoint — the five-step contract
#
# Outbound webhooks are optional and they are notifications, not truth. The
# service's own reconciliation loop credits anything the webhook path missed,
# so a platform that ignores this endpoint entirely and polls instead is
# correct, only slower.
#
# The five steps below are the contract from the integration guide, numbered to
# match it. Step 5 is the whole point: an integration that credits the amount
# out of the webhook body eventually double-credits somebody, because a retried
# delivery looks exactly like a second event.
# ---------------------------------------------------------------------------


@app.post("/platform-webhook")
async def platform_webhook(request: Request, background: BackgroundTasks) -> Response:
    # The signature covers the raw request body bytes. Parse the JSON first and
    # re-serialize it and the whitespace changes, so the signature can never
    # match again. Ask the framework for the bytes: FastAPI and Starlette
    # `await request.body()`, Django `request.body`, Flask `request.get_data()`.
    raw = await request.body()

    # STEP 1 — verify the signature, 401 if it fails.
    #
    # `parse_event` does the three things that are easy to get wrong: it signs
    # over the raw bytes, compares in constant time, and enforces the five
    # minute timestamp window so a captured request cannot be replayed
    # tomorrow. An unknown `type` is not a failure — a newer server may send an
    # event this version has never heard of, and the right answer is to
    # acknowledge it and move on.
    try:
        event = parse_event(raw, request.headers, secret=WEBHOOK_SECRET)
    except WebhookVerificationError:
        return Response(status_code=401)
    except UnknownEventTypeError:
        return Response(status_code=200)

    # STEP 2 — dedup on the event id.
    if not STORE.remember_event(event["id"]):
        return Response(status_code=200)

    # STEP 3 — return 200 immediately.
    #
    # Anything slow goes to a background task, which runs *after* the response
    # is sent. A handler that does its work first will eventually time out
    # under load, and the service will then retry a delivery that in fact
    # succeeded. Ten retries over about three days, then a dead letter and an
    # alert for an operator who has nothing to fix.
    background.add_task(_apply, event)
    return Response(status_code=200)


def _apply(event: PlatformEvent) -> None:
    """STEP 4 and STEP 5 — re-read the resource, act on what the GET said."""
    if event["type"] == "deposit.settled":
        # STEP 4. Note what is *not* used: `event["data"]["amount_credited"]`.
        # It is there, and it is correct, and reading it is still the habit
        # that breaks integrations.
        deposit = CPA.get_deposit(event["data"]["deposit_id"])
        # STEP 5. Act on the GET. In a real platform this is where the order is
        # released, the game credits are granted, the invoice is marked paid —
        # keyed on the deposit id so that doing it twice is harmless anyway.
        STORE.note(
            deposit.external_user_id,
            f"deposit settled: {deposit.amount_credited} {deposit.asset} credited",
            source="webhook",
        )
    elif event["type"] == "withdrawal.completed":
        withdrawal = CPA.get_withdrawal(event["data"]["withdrawal_id"])
        STORE.note(
            withdrawal.external_user_id,
            f"withdrawal complete: {withdrawal.amount_net} {withdrawal.asset} sent",
            source="webhook",
        )
    else:
        # Every other event is a status change this demo only records. Your
        # platform will care about some of them — `deposit.review_required`
        # deserves a message to the user, `withdrawal.failed` deserves one to
        # an operator.
        STORE.note(
            event["data"]["external_user_id"],
            f"{event['type']} ({event['data']['status']})",
            source="webhook",
        )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """This process only. It says nothing about the processing API."""
    return {"status": "ok"}
