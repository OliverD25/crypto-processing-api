# Security policy

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email the maintainer address in the
commit history if that is unavailable.

Useful to include: what an attacker gains, the steps to reproduce, and the
version or commit. A proof of concept helps but is not required — a clear
description of the flaw is enough.

Expect an acknowledgement within a few days. This is a small open-source
project maintained by volunteers; there is no on-call rotation. If the issue is
being actively exploited, say so in the first line.

## Scope

In scope, and taken seriously:

- anything letting a caller move money they do not own
- anything letting a ledger invariant be violated (double credit, overdraft,
  a hold released twice)
- authentication or signature verification bypass on any endpoint
- injection, or a way to make the service act on unverified external input

Out of scope, and already documented as accepted residual risk in
[`docs/operating/security.md`](docs/operating/security.md):

- compromise of the BTCPay host itself. The mitigation is a small hot wallet
  float; that is stated in the README on purpose.
- root on the VPS rewriting the database between invariant checks.
- denial of service against a single-tenant service on one small box.
- the fact that USDT deposit attribution is heuristic because the BTCPay USDt
  plugin reuses addresses from a pool.

If you think one of those is worse than the documentation claims, that is a
finding in itself and worth reporting.

## Supported versions

Pre-1.0. Only the latest tag receives fixes.

## Disclosure

Coordinated. A fix is released, then the advisory is published with credit
unless you prefer otherwise. Given the deployment model — self-hosted, one
operator per instance — the advisory will say plainly what an operator must do,
not just what changed.
