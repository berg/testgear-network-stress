# testgear-network-stress

Network stress and conformance checks for VISA implementations, over HiSLIP and
VXI-11, with no instrument required.

Two things make this different from a normal test suite.

**It brings its own instrument.** `server/` is a HiSLIP and VXI-11 server built
on protocol code vendored from [ugpibd][ugpibd] — a daemon that serves real
GPIB hardware to real VISA clients — backed by a simulated IEEE-488.2 device.
A mock written alongside the checks tends to encode the same misreading of the
spec twice and then agree with itself. This code has an independent reason to
be correct.

**It injects the faults a bench cannot.** A connection that dies mid-reply, a
reply arriving one byte per TCP segment, a socket that goes quiet with the
client still waiting — these happen on a real bench about once a year and never
on demand. Here each is a knob, applied by a proxy in front of an unmodified
server, so the fault lands where the client actually meets it.

## Running

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e /path/to/pyvisa-py
./run_all.sh                                    # everything, both transports
./.venv/bin/python checks/conformance.py        # one script
./.venv/bin/python checks/conformance.py --protocol vxi11
```

`run_all.sh` sweeps both transports by default, because most of what this suite
has turned up is a difference between them. `REPORTS=dir ./run_all.sh` also
writes a JSON report per script; `SOAK=300 ITER=2000 ./run_all.sh` leans on it
harder.

The Rust server builds itself on first use; you need a Rust toolchain
([rustup.rs](https://rustup.rs)) or a prebuilt binary in
`TESTGEAR_MOCK_SERVER`.

Against real hardware, name a resource and the mock never starts:

```bash
./.venv/bin/python checks/conformance.py -r TCPIP0::10.0.0.5::hislip0::INSTR
```

## Pointing at a specific pyvisa-py tree

```bash
./run_all.sh --pyvisa-py ~/code/pyvisa-py
./.venv/bin/python checks/conformance.py --pyvisa-py ~/code/pyvisa-py
```

Every script honours `--pyvisa-py` (or `TESTGEAR_PYVISA_PY`) identically. It
takes precedence over an editable install, applies to the subprocesses
`run_all.sh` spawns, and **fails immediately** if the path is not a pyvisa-py
checkout rather than falling through to whatever happens to be installed --
silently testing the wrong tree is worse than not running.

Every run prints the tree and the commit it actually loaded. A result that
cannot name what produced it is not reproducible, and comparing trees is the
point.

## Comparing backends

Every check is written against the VISA API, not against pyvisa-py, so the same
run can be pointed at another implementation:

```bash
./.venv/bin/python checks/conformance.py --backend ni     # NI-VISA
./.venv/bin/python checks/conformance.py --backend rs     # R&S VISA
./.venv/bin/python checks/conformance.py --backend /path/to/libvisa.so
```

This is what turns a failure into a **disparity**, which is a much stronger
claim: not "this behaviour is undesirable" but "this behaviour is inconsistent
with a shipping implementation of the same spec".

`compare.py` runs the same checks across several of them and prints the matrix,
marking the rows where they disagree:

```bash
./.venv/bin/python compare.py --backends py,ni --protocol vxi11
./.venv/bin/python compare.py --pyvisa-py-trees main=/a,branch=/b --html out.html
```

The second form compares two checkouts of pyvisa-py instead of two VISA
libraries, which answers "did my branch change anything?" -- against upstream
`main` this branch currently comes out ahead on six checks.

The same question across two runs made at different times is
`tools/outcomes.py`. It reduces a run to one sorted line per check -- outcome
and name, none of the measurements -- so two runs diff with plain `diff`, and
`--against` names each change for what it is:

```bash
./run_all.sh --pyvisa-py ~/code/pyvisa-py            # REPORTS=after
./.venv/bin/python tools/outcomes.py after --against before
```

It reads whatever a run left behind: `run_all` per-script reports, a
`compare.py --json` file, or a CI run's `site/columns`. Every CI run also ships
`site/outcomes.txt` in its site artifact, which is the same dump ready to diff.

### Running the vendor implementations in containers

NI and R&S ship x86-64 Linux binaries only, so they run on a Linux host rather
than on a development Mac -- emulating x86 to measure an implementation whose
*timing* is part of what is being measured would be measuring qemu.

Drop the installers in `vendor/` (see [`vendor/README.md`](vendor/README.md))
and:

```bash
./remote-compare.sh --check     # what is installed, and does it load?
./remote-compare.sh             # build, run, and print the matrix
```

It syncs the suite and the pyvisa-py tree under test to `$TESTGEAR_HOST`
(default `slopbox`), builds one container image per implementation, runs the
checks inside each, collects the JSON reports and renders the matrix. The tree
under test is mounted rather than built into the image, so a new commit does
not invalidate the layer that pulls a gigabyte from ni.com.

The same containers are what CI runs, one implementation per job, publishing
the matrix to GitHub Pages -- see [`docs/ci.md`](docs/ci.md). `remote-compare.sh`
stays as the offline route.

pyvisa-py's own column is produced **in the same container** rather than on the
local machine. Running it here would compare a Linux VISA against a macOS one
and quietly attribute the platform difference to the implementation.

The container check is a diagnosis step in its own right: a vendor library that
installs but will not initialise -- NI-VISA wanting kernel modules a container
cannot provide is the likely case -- reports exit 11 with that stated, rather
than surfacing later as "backend not available", which is indistinguishable
from having forgotten to install it.

| id | Implementation | Availability |
| --- | --- | --- |
| `py` | PyVISA-py | pip |
| `ni` | NI-VISA | vendor installer; macOS, Linux, Windows |
| `rs` | R&S VISA | vendor installer; macOS, Linux, Windows |
| `keysight` | Keysight IO Libraries | vendor installer; Linux and Windows only |
| `tek` | TekVISA | vendor installer; Windows only, so CI cannot reach it |
| `sim` | PyVISA-sim | pip; no network, so API shape only |

A backend that is not installed is reported with what to install and where to
get it, and the run stops. It is never quietly skipped: a comparison table with
a column silently absent reads like agreement between the backends that remain.

## Skips are not passes

A check that cannot run reports `SKIP`, is counted separately, and is listed
again in the summary. This matters more than it sounds. In the suite this one
grew out of, the large-reply checks stayed skipped against an HP 34401A through
the entire development of that suite without anyone noticing, because at a
glance a skipped check reads like a passing one.

## Layout

| Path | What it is |
| --- | --- |
| `server/` | The Rust mock server. Vendored protocol code plus the virtual instrument, fault injector and control channel. |
| `testgear/` | The Python harness: backend selection, the server fixture, PASS/FAIL/SKIP bookkeeping, HTML rendering, and the entry point every check script shares (`testgear/script.py`). |
| `checks/` | The checks themselves: seventeen numbered scripts plus two conformance suites. See [Writing a check](#writing-a-check). |
| `reproducers/` | One runnable script per open finding, and the original bench diagnostics under `bench/`. |
| `docs/findings.md` | What this suite has found, and what looked like a finding and was not. |
| `run_all.py`, `compare.py` | The suite runner and the cross-backend matrix. `run_all.sh` is a shim over the first; both take their script list from `testgear/suite.py`. |
| `tools/outcomes.py` | A run as one sorted line per check, for diffing two runs; `--against` names what changed. |
| `.github/workflows/` | The same runs in CI, one Linux job per implementation, published to Pages. See [`docs/ci.md`](docs/ci.md). |

## Writing a check

A check is a function with a name, registered with `@check`. The name is what
the cross-backend matrix joins on, so it is a static title of what is being
checked and never varies with the outcome — the evidence goes in what the
function returns, or in the message of the assertion that fails.

```python
@check("viFlush reports a VISA status", rule="VPP-4.3 3.2.4")
def check_flush():
    """An unsupported operation must report VI_ERROR_NSUP_OPER, not raise out
    of the library: a caller cannot catch what it has no reason to expect."""
    lib, sess = io()
    st = visa.status(lib.flush, sess, constants.BufferOperation.discard_read_buffer)
    assert st in (StatusCode.success, StatusCode.error_nonsupported_operation), f"got {st!r}"
    return f"got {st!r}"
```

A check reports by raising: `AssertionError` for a failed expectation, `Skip`
for "cannot run here", anything else for a check that broke. Returning
normally is a pass, and the returned string becomes the detail. `rule=` names
the clause it rests on — a failure that cites a rule is a bug report, one that
does not is an opinion. `protocols=` limits it to the transports it makes
sense for, and `watchdog=` overrides the file's timeout for a check that
legitimately takes longer (`watchdog=0` turns it off).

Three optional module-level names, all read by `testgear/script.py`:

| Name | What it does |
| --- | --- |
| `CTX` | Filled in with the backend, resource, protocol, args and live `Stats` before any check runs. |
| `SETUP(ctx)` | A context manager entered around the whole file, for a session or state the checks share. |
| `add_arguments(parser)` | Options this script adds to the shared parser. |

Then the whole entry point is:

```python
if __name__ == "__main__":
    script.run()
```

Nothing is passed in that can be looked up: the module is the caller's, the
title comes from the filename, and which transports the script belongs to
comes from `testgear/suite.py`, which already had to know. **Add the script to
`SCRIPTS` there** — one that is missing runs for whoever invokes it directly
and for nobody else: not in `run_all.py`, not in the matrix, not in CI.
`script.run()` says so on stderr if you forget.

For a family of checks built in a loop, `harness.registrar` gives each one a
distinct identity and an explicit position, which a plain factory cannot:

```python
def _register_chunk_checks() -> None:
    add = harness.registrar(globals())
    for chunk in (1, 7, 64, 997):
        add(_intact(chunk), f"a large message read {chunk}B at a time is intact",
            rule="VPP-4.3 RULE 6.1.2")
```

## Reports

`--report PATH` writes JSON, `--html PATH` writes a self-contained page --
both rendered from the same records, so they cannot disagree about what
happened. Failures and skips come first and open; passes fold away. A report is
read to find out what went wrong, and forty green lines above the one red one
buries its own point.

The mock server is driven over a line-JSON control socket, deliberately not
carried in either instrument protocol — arming a fault over the stream under
test would show up in the observation log the check then reads back.

## Specs

Checks cite the clause they rest on: VPP-4.3 (*The VISA Library*), VXI-11 Rev
1.0 (*TCP/IP Instrument Protocol Specification*), IVI-6.1 (*HiSLIP*), IEEE
488.2 and SCPI-99. The documents are IVI Foundation and VXIbus Consortium
copyright and are **not** included here; a citation names the clause and you
read it in your own copy.

Citations are not decoration. Point `tools/spec_rules.py` at your copies and it
extracts every normative statement, cross-references them against the `rule=`
annotations on the checks, and reports what is covered:

```bash
./.venv/bin/python tools/spec_rules.py --specs ~/specs --out docs/spec-coverage.md
```

A check citing no clause counts for nothing in that report, which is
deliberate. When three implementations were first compared, every check they
disagreed about that cited **no** clause turned out to be the check's own
fault, and every check that cited one survived. Seven for seven. So an uncited
check is treated as suspect until a clause is found for it -- not because a
citation makes a check correct, but because writing one forces the question
"what says so?", which is exactly what a check written from one implementation's
behaviour cannot answer.

[`docs/spec-coverage.md`](docs/spec-coverage.md) is the generated report and
[`docs/spec-gaps.md`](docs/spec-gaps.md) is the hand-written queue.

## Licence

GPL-3.0-or-later. Not a style choice: `server/src/{hislip,vxi11,frontend}/` and
`server/src/backend.rs` are vendored from [ugpibd][ugpibd], which is
GPL-3.0-or-later, and anything derived from them inherits it. The Python
harness is under the same licence for consistency.

[ugpibd]: https://github.com/berg/ugpibd
