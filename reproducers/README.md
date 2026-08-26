# Reproducers

Minimal scripts that each demonstrate one thing. They are **not** maintained
checks: nothing here is run by `run_all.sh`, nothing here is expected to keep
passing, and a reproducer that stops reproducing has done its job and can go.

Two kinds live here.

## `./` — current findings

One script per open entry in [`../docs/findings.md`](../docs/findings.md), each
runnable against the mock server with no hardware and no arguments:

| Script | Finding |
| --- | --- |
| `zero_max_recv_size.py` | A `maxRecvSize` of zero wedges a VXI-11 session forever |
| `stalled_connection.py` | A stalled VXI-11 connection reports `VI_ERROR_IO` ~11s late |
| `unimplemented_operations.py` | `viFlush` raises `NotImplementedError`; `VI_ATTR_IO_PROT` is unreadable; a non-default trigger protocol is accepted |

Each prints what it saw and exits non-zero when the finding still reproduces,
so they double as a way to tell whether a fix worked:

```bash
./.venv/bin/python reproducers/zero_max_recv_size.py
./.venv/bin/python reproducers/zero_max_recv_size.py --pyvisa-py /path/to/a/fix
```

## `bench/` — the original bisection scripts

The throwaway scripts used to chase down the findings on the branch this suite
grew out of, kept because they are the shortest statement of what each bug was.

**They do not run here.** They import the old `common` module, hard-code
resource strings for a Keysight M8132A at `192.168.81.74` and an HP 34401A
behind `ugpibd` on localhost, and several depend on instrument-specific
behaviour. They are reference material, not code.

The one worth knowing about is `diag_trigger2.py`: two or more
`viAssertTrigger` calls with no intervening read, followed immediately by
`viClear`, make an M8132A reset the TCP connection. That was verified identical
on upstream pyvisa-py, and the Trigger bytes on the wire are unchanged by the
branch, so it is instrument-side. It is why `08_soak.py` leaves triggers out of
its mix unless `--trigger` is passed.

`diag_connect_and_spin.py` is the VXI-11 counterpart, and the only one here
that reaches below the VISA layer into `pyvisa_py.protocols.rpc` directly.
