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
checks inside each, collects the JSON reports and renders the matrix.

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
| `tek` | TekVISA | vendor installer; Windows only |
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
| `testgear/` | The Python harness: backend selection, the server fixture, PASS/FAIL/SKIP bookkeeping, HTML rendering. |
| `checks/` | The checks themselves: nine numbered scripts plus two conformance suites. |
| `reproducers/` | One runnable script per open finding, and the original bench diagnostics under `bench/`. |
| `docs/findings.md` | What this suite has found, and what looked like a finding and was not. |
| `run_all.sh`, `compare.py` | The suite runner and the cross-backend matrix. |

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

## Licence

GPL-3.0-or-later. Not a style choice: `server/src/{hislip,vxi11,frontend}/` and
`server/src/backend.rs` are vendored from [ugpibd][ugpibd], which is
GPL-3.0-or-later, and anything derived from them inherits it. The Python
harness is under the same licence for consistency.

[ugpibd]: https://github.com/berg/ugpibd
