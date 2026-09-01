# Running this in CI

Two workflows, and a rule that holds both of them up.

| Workflow | What it runs | Publishes | Triggers |
| --- | --- | --- | --- |
| [`pyvisa-py.yml`](../.github/workflows/pyvisa-py.yml) | the pyvisa-py column only, both transports, Linux and Windows | no | `pull_request`, `workflow_dispatch` (repo + ref), nightly |
| [`full-run.yml`](../.github/workflows/full-run.yml) | every implementation, both transports | GitHub Pages | weekly, `workflow_dispatch` |

Everything else is a reusable workflow the two of them call: `_plan`,
`_leg-linux`, `_leg-windows`, `_aggregate`, `_publish`.

## The rule

**Nothing that runs third-party code holds a credential.**

`pyvisa-py.yml` clones a repository somebody names and executes it. That is
what it is *for* — pointing it at a fork's PR branch is the whole feature — and
it is why it has no vendor leg, no AWS role and no Pages token.

The hazard is not fork pull requests specifically. A `workflow_dispatch` with a
`repo` input is the same hazard arriving through a different door. So the rule
is about the workflow rather than the event, and it is enforced structurally:

- `full-run.yml` **takes no repo or ref input at all**. It hardcodes
  `pyvisa/pyvisa@main` and `pyvisa/pyvisa-py@main`. Testing a branch is what
  the other workflow is for.
- `tools/gha_matrix.py` emits no `vendor: true` leg for a `pyvisa-py` run, so
  no job that would assume the role is ever created.
- The vendor legs enter the `vendor-drivers` GitHub Environment, whose
  deployment branches are `main` only. A fork PR runs at `refs/pull/N/merge`,
  matches no branch policy, and is stopped before the job starts.
- The role's OIDC trust policy requires
  `repo:OWNER/REPO:environment:vendor-drivers` as an exact string, so that
  branch rule is enforced by **AWS**, not only by YAML a pull request could
  edit.
- AWS credentials are cleared from the environment immediately after the
  installer is fetched, before anything third-party runs.

**Never use `pull_request_target`.** It runs base-repo workflow code with a
writable token and repository secrets, against a pull request's source tree —
which this suite installs and executes. It would be an unauthenticated handover
of a write token.

## The other rule

**Vendor installers must not leave the job that fetched them.**

On a public repository, workflow artifacts and Actions caches are readable by
anyone, and caches on the default branch are readable by fork-PR workflows. So
for anything under `vendor/`, or any image layer containing it:

- no `actions/upload-artifact`
- no `actions/cache`
- no buildx `type=gha` cache
- no registry push, including a public GHCR package

This is the rule most likely to be undone by a well-meaning speed
optimisation — the NI image takes eight to twelve minutes to build, and caching
it is the obvious thing to reach for. The `py` image has nothing proprietary in
it and *is* safe to cache; the vendor ones are not. If the build time becomes
intolerable, the answer is a **private** GHCR package pushed only from `main`,
not a cache.

## What makes a run red

Neither runner's exit status can answer this. `compare.py` always exits 0 on
purpose — disagreement is the finding, not an error — and `run_all` exits with
a count of failed scripts, which conflates a check failing with the bench going
away. `tools/ci_status.py` decides, from the columns and the exit-code
sidecars.

| Exit | Meaning | Red? |
| --- | --- | --- |
| 1 | a check failed | **no** — that is the product; see [findings.md](findings.md) |
| 2 | unexpected exception | yes — the suite is broken, never a finding about a backend |
| 3 | the target went away | no — flagged as flaky. A shared cloud runner is a flakier bench than a desk, and a flaky bench reported as a library regression wastes the next person's afternoon |
| 4 | setup error | no — reported as an unavailable column |
| 5 | this suite made a bad VISA call | yes — our bug |

Exit 2 should be rare, and getting there took work. `run_checks` has always
turned an exception inside a registered check into a FAIL, but the imperative
setup *between* checks had no such net: a library that raised where the spec
says it returns a status took the whole script with it, and every check after
it vanished from the column — which reads as "not applicable" rather than as a
failure. `Stats.attempt` is that net, and the three scripts that were aborting
against upstream pyvisa-py main now record the raise as a FAIL, cite the clause
it breaks, and carry on. One of those crashes turned out to be concealing four
further failures behind it.

So exit 2 now means what it says: something broke that no check was watching.
If a new one appears, the fix is usually to put the offending call inside an
`attempt` and give it a clause, not to catch it more broadly.

Plus a regression against `docs/ci-baseline.json`: a check the baseline records
as passing that now fails **or now skips**. A new skip counts, deliberately —
in the suite this one grew out of, the large-reply checks stayed skipped
through an entire development cycle unnoticed, because at a glance a skipped
check reads like a passing one.

The baseline is not committed yet. Generating one here would be macOS outcomes
judging Linux runs. The first green run on `main` should write it:

```bash
tools/ci_status.py --columns site/columns --write-baseline
```

Until then the gate says so and passes. A full run never gates at all — its
output is the page, not a verdict.

## Columns that did not run

Every leg uploads with `if: always()` and the aggregate runs with `if:
always()`. `tools/normalise_reports.py` builds its index from the **plan**, not
from what turned up, so an implementation that was meant to run and produced
nothing gets a column carrying its reason, on the page and in the Markdown.

This is not politeness. A comparison table with a column silently absent reads
like agreement between the backends that remain, and the disagreement tally
ignores non-`ok` columns so that one dead runner cannot fabricate two hundred
disparities.

## Why the Linux legs stay in the container

Four reasons, each sufficient on its own:

1. **Port 111.** `testgear/server.py` probes a privileged bind to decide
   whether to run the portmapper. On a bare runner the job is the unprivileged
   `runner` user, the probe fails, and the VXI-11 resource becomes
   `TCPIP0::host,port::inst0::INSTR` — a pyvisa-py-only extension that NI and
   R&S reject. The entire vendor VXI-11 half of the matrix would disappear.
2. **glibc.** The Dockerfile builds the mock on bullseye to pin glibc 2.31
   against the Ubuntu 22.04 runtime. A binary built on `ubuntu-latest` would
   not start, and that reads as a broken mock rather than a host mismatch.
3. **NI's install** needs the `systemctl` stub and its daemons — and
   `entrypoint.sh` distinguishes *library not found* (exit 10) from *found but
   would not initialise* (exit 11). That is a diagnosis, not a detail.
4. **pyvisa-py's own column** comes from an image built exactly like the
   vendors': same base, same Python, same suite. A difference between columns
   is then a difference between implementations rather than between two
   machines.

The mock server is **not** prebuilt for Linux and injected, for reason 2. It
*is* prebuilt on Windows, where the target differs anyway and `docs/windows.md`
already documents `TESTGEAR_MOCK_SERVER` as the skip-the-toolchain route.

## Why pyvisa-py runs on Windows too

Keysight and TekVISA run on Windows against a Windows mock, while pyvisa-py's
other column runs on Linux. A row where they differ may be differing about the
*platform*. `04_concurrency`'s descriptor-leak check is the known case: it
SKIPs on Windows for want of `/dev/fd`.

The Windows pyvisa-py column costs nothing — no driver to install — and gives
every Windows disagreement a same-OS control. It is the difference between a
defensible claim and a suggestive one.

For the same reason, "failures unique to PyVISA-py" requires *every* pyvisa-py
column to fail. One that fails on Linux and passes on Windows is telling you
about the platform, and does not belong under confirmed findings.

## Still unverified

Stated plainly, because none of it has run yet:

- **The workflows have never executed.** `actionlint` is clean; that is all.
- **The container changes have not been built.** There is no Docker on the
  machine they were written on. `docker/Dockerfile` now expects the tree under
  test to be *mounted* at `/pyvisa-py` rather than copied in.
- **Keysight and TekVISA silent installs.** Both are InstallShield-family
  bundles; the flags in `manifest.json` are placeholders. Verify them by hand
  on a throwaway Windows VM before trusting those legs, and note that a hosted
  runner cannot reboot — `install_vendor_windows.py` treats exit 3010 as
  success and starts the named services, and if the library still will not
  initialise the leg reports an unavailable column, which is the honest
  outcome. If unattended install turns out to be impossible, the fallback is a
  self-hosted Windows runner with the drivers already installed: `runs_on` is
  a field in `tools/gha_matrix.py` so that is a one-line change. Such a runner
  must never accept fork-PR jobs — already true, since vendor legs never run
  on that path.
- **`environment: ""`** is used to mean "no environment" for the non-vendor
  legs. Confirm on the first run that a `py` leg is not gated.

## Provisioning

See [`../infra/aws/REQUIREMENTS.md`](../infra/aws/REQUIREMENTS.md).
