# Running this in CI

Two workflows, and a rule that holds both of them up.

| Workflow | What it runs | Publishes | Triggers |
| --- | --- | --- | --- |
| [`pyvisa-py.yml`](../.github/workflows/pyvisa-py.yml) | the pyvisa-py column only, both transports, Linux and Windows | no | `pull_request`, `workflow_dispatch` (repo + ref), nightly |
| [`full-run.yml`](../.github/workflows/full-run.yml) | every implementation, both transports | GitHub Pages | weekly, `workflow_dispatch` |

Everything else is a reusable workflow the two of them call: `_plan`,
`_leg-linux`, `_aggregate`, `_publish`.

**Everything runs Linux.** All four implementations compared -- pyvisa-py,
NI-VISA, R&S VISA and Keysight IO Libraries -- have 64-bit Linux builds, so
they run in one image family on one kernel against one mock. A difference
between two columns is then a difference between two implementations, which is
the only kind of difference worth publishing. TekVISA is Windows-only and has
no column; that costs one independent implementation and buys a same-OS
comparison for the three that remain.

## What is actually being protected

Not secrets. The bucket holds NI, R&S, Keysight and TekVISA installers, which
are freely downloadable from the vendors by anyone willing to click through a
licence page. Nothing in it is confidential.

What the licences do not permit is **redistribution**, and this repository is
public. So the property worth keeping is narrow and specific: those files, and
any image layer containing them, must not end up somewhere that hands them to
the world. That is a licensing constraint, not a security perimeter, and the
arrangement below should be read as tidiness rather than as a defence against
an adversary.

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
  branch rule is enforced by AWS as well as by GitHub.
- AWS credentials are cleared from the environment immediately after the
  installer is fetched, before anything third-party runs.

None of that is load-bearing against a determined attacker, and it is not meant
to be: the credential reads four installer files and can write nothing. It is
cheap, so it is done. The reason to keep it is that a role which *could* only
ever read those files is easy to reason about, and one that has drifted into
holding something else is not.

Two things this deliberately does **not** do, because the value does not
justify them here: `main` is not branch-protected, and the environment carries
no required reviewer. Both would make sense for a bucket that held secrets.
This one does not.

**Never use `pull_request_target`.** It runs base-repo workflow code with a
writable token and repository secrets, against a pull request's source tree —
which this suite installs and executes. It would be an unauthenticated handover
of a write token.

## The other rule

**Vendor installers must not leave the job that fetched them.**

This is the rule that matters, and it is a licensing one. On a public
repository, workflow artifacts and Actions caches are readable by anyone --
so uploading a vendor installer to one *is* redistributing it, to precisely
the audience the licence is about. For anything under `vendor/`, or any image
layer containing it:

- no `actions/upload-artifact`
- no `actions/cache`
- no buildx `type=gha` cache
- no registry push, including a public GHCR package

Of the two rules here this is the one to actually enforce, and it is the one
most likely to be undone by a well-meaning speed optimisation — the NI image takes eight to twelve minutes to build, and caching
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

The mock server is **not** prebuilt and injected, for reason 2: the Dockerfile
pins it to glibc 2.31 on purpose, and a runner-built binary would undo that.

## Still unverified

Stated plainly, and kept current -- the list shrinks as things get run.

What *has* been exercised: the `py` leg end to end on GitHub's runners over
four runs, the aggregate, the Markdown summary and the baseline gate in both
directions; and the `py` image built and run under podman, where the mounted
`/pyvisa-py` tree, the provenance block and the port-111 portmapper bind were
all confirmed. What has not:

- **No vendor leg has ever run.** The `ni`, `rs` and `keysight` images have
  never been built, in CI or anywhere else. Podman on the machine they were
  written on is arm64 and every vendor ships x86-64 Linux only, so the first
  real exercise of that path is the first `full-run` after this merges.
- **`full-run.yml` has never executed at all**, because a `workflow_dispatch`
  workflow has to be on the default branch before it can be dispatched.
- **The Keysight Linux install step.** Written against what Keysight are known
  to ship — a tarball with an installer script, loose `.deb`s, or a `.run` —
  and never run, because the download was not in hand when it was written. It
  reports which shape it found and lets the run continue either way, so a wrong
  guess is an unavailable column rather than a failed build. Trim it to the one
  branch that is actually needed once the real package exists.
- **`environment: ""`** is used to mean "no environment" for the non-vendor
  legs. Confirm on the first run that a `py` leg is not gated.

## Provisioning

See [`../infra/aws/REQUIREMENTS.md`](../infra/aws/REQUIREMENTS.md).
