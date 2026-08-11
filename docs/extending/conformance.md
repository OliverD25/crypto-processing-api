# The conformance suite

The asset-extension contract in executable form. Prose describing what a
backend must do is a wish; these classes are the same statements as tests, and
they ship **inside the installed package** so a fork can run its own backend
against the identical suite CI runs.

[Adding your own asset](adding-an-asset.md) is the guide. This page is about
the suite itself: what it asserts, how to run it, and why you may not edit it.

## Running it

Subclass the contract class and supply the fixtures it asks for. Nothing else:
every test is inherited.

```python
from crypto_processing_api.testing.contracts import AutomatedBackendContract


class TestMyBackend(AutomatedBackendContract):
    @pytest.fixture
    def backend(self, my_gateway):
        return MyPayoutBackend(my_gateway)

    @pytest.fixture
    def withdrawal(self, session):
        return make_a_withdrawal(session)

    @pytest.fixture
    def simulate_completion(self, my_gateway):
        return lambda ref: my_gateway.finish(ref)
```

`tests/integration/test_backend_contracts.py` is the worked example. Both
shipped backends subclass the same classes you will, so reading it tells you
what a real set of fixtures looks like.

The suite imports nothing from `tests/`, on purpose. A contract that only runs
inside this repository's test tree is not a contract anybody else can use.

## The five contracts

| Class | For | Fixtures you supply |
|---|---|---|
| `AutomatedBackendContract` | a backend that sends money by itself | `backend`, `withdrawal`, `simulate_completion` |
| `OperatorBackendContract` | a backend where a human sends the money and code verifies it | `backend`, `paying_transaction`, `near_misses` |
| `FeePolicyContract` | the arithmetic the ledger depends on | `policy`, `workable_gross`, `dust_gross` |
| `CustodySourceContract` | "how much do we actually hold" | `source`, `broken_source` |
| `EndToEndLedgerContract` | the books, with your backend plugged in | `run_withdrawal` |

What each one is asserting, in one line apiece:

**`AutomatedBackendContract`** — a reference that survives a crash and can be
found again; unclaimed payouts to the same destination are visible, so nothing
pays twice; polling changes nothing; no gateway vocabulary escapes into the
state machine; a completed payout reports a transaction id; amounts are never
floats; the payout carries our correlation metadata, so recovery resolves by
echoed id rather than by guessing from destination and amount; and `cancel`
answers a boolean and never raises.

**`OperatorBackendContract`** — references are unique, because two withdrawals
sharing one would settle each other; the exact paying transaction verifies;
every named near-miss is refused (wrong recipient, wrong amount, wrong sender,
wrong token); an unknown transaction is refused; and confirmations never go
negative, because a reorg can put the chain tip behind a block you already saw.

**`FeePolicyContract`** — net plus fee equals gross; committed equals net plus
the wallet fee; nothing is ever negative; the fee never exceeds the amount;
every number is an integer; and dust is refused at quote time, so a doomed
withdrawal never takes a hold.

**`CustodySourceContract`** — a healthy source reports integer units and names
itself, and an unavailable one answers `None`, never `0`. That last assertion is
the most important line in the file. Zero means "we hold nothing", which is an
insolvency emergency; an unreachable API means "we do not know". A source that
returns zero when its upstream is down pages somebody at 3am about a wallet
that is perfectly fine, and — far worse — trains them to ignore the alert.

**`EndToEndLedgerContract`** — the one that matters. Drive a real withdrawal
from hold to settle through a real ledger with your backend plugged in, then
assert the books still balance and every custody line still reconciles. The
four contracts above check a backend in isolation; this checks the claim
anybody actually cares about.

## Passing it unmodified is the acceptance test

If you find yourself wanting to change `contracts.py` to make your backend
pass, that is the contract telling you something. Every assertion in it is
about a property the money path already relies on — none of them are style. If
one fails, something in `services/` will misbehave in a way that costs coins,
and the docstring on each test says which.

The same rule holds for this repository. The two shipped backends
(`BtcpayPayoutBackend`, automated; `ManualTronBackend`, operator-verified) both
run the suite in CI, so a change to the contract that breaks either is caught
before it reaches a third one.
