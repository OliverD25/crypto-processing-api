# Example application

!!! note "Not written yet"

    A worked platform integration — `examples/platform-demo/` — is the next
    milestone. This page is its reserved home, and it is here rather than
    absent so that nothing has to be renamed when it lands.

What it will be: a single-file FastAPI application with Jinja2 and HTMX,
written to be read start to finish, using the
[Python client](sdks.md). A fake login, a BTC deposit with polling, balances, a
withdrawal, and a `/platform-webhook` endpoint implementing the five-step
contract from [Integrating](index.md#handling-an-event).

It will run as an opt-in profile on the regtest stack, and its whole loop will
run in the [nightly job](../operating/nightly-e2e.md). A tutorial that rots
fails in front of the worst possible reader — somebody meeting the project for
the first time — so the nightly is what keeps it honest.

Until it exists, the closest thing to a worked example is the drill script
`scripts/dev/smoke_test.py`, described in the
[regtest walkthrough](../getting-started/regtest.md). It is a test rather than
a tutorial, but it does the whole loop against a real BTCPay and asserts every
number.
