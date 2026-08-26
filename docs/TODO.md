# Still to do

Roughly in the order it makes sense to tackle. Nothing here is started.

## Port the rest of the checks

The bulk of the original suites, ~5,000 lines across two directories:

- The 9 numbered HiSLIP scripts (`01_smoke` .. `09_remote_local`): query
  storms, large reads, SRQ storms via queue and handler, concurrency and leak
  checks, lock cycling and contention, `viTerminate` against blocked reads,
  device clear mid-message, the randomised soak, REN/GTR/LLO verified by
  effect rather than by status code.
- The full 30-check VXI-11 conformance set: error codes, operation flags,
  locking, liveness, session behaviour.
- `reproducers/`: the `diag_*` bisection scripts, as minimal repros for
  specific findings rather than maintained checks.
- `run_all.sh`.

## Cross-backend comparison

`compare.py`: run the same checks against several backends and emit the
disparity matrix. The JSON report plumbing is already in place for it; what is
missing is the runner and the rendering.

## HTML reporting output

A run currently produces terminal output and, with `--report`, a JSON blob.
Neither is what you want to hand somebody: the terminal output scrolls off,
and the JSON is a serialisation format rather than something to read.

Wanted: an HTML report rendered from the same structured `Result` records the
JSON already carries, so the two never disagree about what happened.

- One run: checks grouped as they are in the source, each with its outcome,
  its cited rule, its duration and its detail line. Failures readable without
  expanding anything; passes collapsible.
- The provenance block up top -- backend, pyvisa-py tree and commit, python,
  platform, resource -- since a report that cannot name what produced it is
  not evidence.
- Skips visible as their own state, not styled as a muted pass. The whole
  reason skips are tracked separately is that they read like passes at a
  glance, and colour is exactly where that mistake gets made again.
- Several runs: the disparity matrix from `compare.py` as a table, checks
  down the side and backends across the top, with the cells that disagree
  being the thing the eye lands on first.

Self-contained output -- one file, no external assets -- so it can be
attached to an upstream issue or opened from a USB stick on a bench machine
with no network.
