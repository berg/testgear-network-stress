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
./.venv/bin/python checks/conformance.py                    # HiSLIP, mock server
./.venv/bin/python checks/conformance.py --protocol vxi11
```

The Rust server builds itself on first use; you need a Rust toolchain
([rustup.rs](https://rustup.rs)) or a prebuilt binary in
`TESTGEAR_MOCK_SERVER`.

Against real hardware, name a resource and the mock never starts:

```bash
./.venv/bin/python checks/conformance.py -r TCPIP0::10.0.0.5::hislip0::INSTR
```

## Pointing at a specific pyvisa-py tree

`PYTHONPATH` wins over an editable install, so branches can be compared without
reinstalling anything:

```bash
PYTHONPATH=/path/to/some/pyvisa-py ./.venv/bin/python checks/conformance.py
```

Every run prints the tree and commit it actually loaded. A result that cannot
name what produced it is not reproducible, and comparing trees is the point.

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
| `testgear/` | The Python harness: backend selection, the server fixture, PASS/FAIL/SKIP bookkeeping. |
| `checks/` | The checks themselves. |

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
