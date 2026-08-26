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

## Make an out-of-tree pyvisa-py checkout trivial to use

Running the whole suite against a pyvisa-py tree checked out somewhere else
should be one obvious thing, not a `PYTHONPATH` incantation the caller has to
know wins over the editable install.

`PYTHONPATH=/path/to/pyvisa-py ...` already works and the provenance block
already reports which tree was loaded, so this is about the ergonomics, not
the mechanism. Wanted:

- A first-class way to name the tree -- a `--pyvisa-py /path` flag, a
  `TESTGEAR_PYVISA_PY` env var, or a positional argument to `run_all.sh` the
  way the old `vxi11-stress/run_all.sh` took one -- that every check script
  and the suite runner honour identically.
- It should work for several trees in one session without reinstalling
  anything, since comparing branches is the point.
- Whatever it resolves to must show up in the provenance block, and a tree
  that does not exist or has no `pyvisa_py/` in it should fail immediately
  with a message naming the path, not fall through to whatever happens to be
  installed. Silently testing the wrong tree is worse than not running.

## Cross-backend comparison

`compare.py`: run the same checks against several backends and emit the
disparity matrix. The JSON report plumbing is already in place for it; what is
missing is the runner and the rendering.

## Classify the two open VXI-11 results

Neither is a finding yet:

- A stalled connection is reported as `VI_ERROR_IO` rather than a timeout.
  Adjacent to the socket-deadline work on the `network-robustness` branch.
- Concurrent sessions time out.

Bisect both against the mock before calling either one. Three harness bugs
found on day one each presented as a client-side transport bug.

## Server-side disparities worth writing down

Differences that belong to the *server*, not the client, and so cannot be
asserted in a client suite -- but are worth a `docs/findings.md`:

- HiSLIP synthesises the status byte server-side and leaves the rest to the
  SRQ forwarder, so a status forced at the device never reaches a HiSLIP
  client. VXI-11 `device_readstb` maps onto a real serial poll and does.
- An empty bus read is a timeout over VXI-11 and a deliberate I/O error over
  ugpibd's HiSLIP, on the grounds that a plausible empty string is worse than
  a loud failure.
