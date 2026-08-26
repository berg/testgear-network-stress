# Still to do

Everything originally on this list is done: the nine numbered scripts and both
conformance suites are ported, `--pyvisa-py` points at an out-of-tree checkout,
`compare.py` produces the cross-backend matrix, HTML reporting exists, the
reproducers are in place, and the open results are classified in
[`findings.md`](findings.md).

What is left is what running it turned up.

## Get a second implementation into the matrix

The comparison machinery works and has only ever been run with pyvisa-py in it
-- one column against another checkout of itself. Every disparity claim in
`findings.md` is currently "pyvisa-py disagrees with the spec", which is a
weaker argument than "pyvisa-py disagrees with NI-VISA".

NI-VISA and R&S VISA both have macOS builds and both are free. Installing
either and running `./compare.py --backends py,ni --protocol vxi11` would
settle several open entries at once -- in particular whether the ~11s floor on
a stalled connection and the unacknowledged `device_intr_srq` are pyvisa-py's
alone.

## Take the findings upstream

Five open client-side entries, in the order they are worth raising:

1. `maxRecvSize` of zero wedges a VXI-11 session forever. A bounds check is
   the whole fix, and an unkillable loop inside a library call is the most
   serious thing here.
2. VXI-11 interrupts are unacknowledged, capping service requests at one per
   second against a server that waits for the reply.
3. A stalled connection reports `VI_ERROR_IO` about 11s late.
4. `viFlush` raises `NotImplementedError` out of a VXI-11 session.
5. `viAssertTrigger` accepts a trigger protocol VXI-11 cannot express, and
   `VI_ATTR_IO_PROT` is unreadable there.

Each has a reproducer that exits non-zero while it still stands, so a patch can
be checked against it directly.

## Smaller things

- The HiSLIP side has no equivalent of `vxi11_conformance.py`. The RPC-level
  fault injector is VXI-11 only; the HiSLIP message framing would need its own,
  and there are conditions -- a message type the client does not expect, a
  wrong message-parameter field -- that nothing currently reaches.
- `08_soak.py` is excluded from `compare.py`, correctly: a randomised workload
  compares badly. A seeded, fixed-length variant that produced the same
  operation sequence on every backend would compare fine and is not hard.
- The suite has never run on Linux or Windows. Nothing in it is
  platform-specific by design, which is exactly the kind of belief that turns
  out to be wrong the first time somebody tries.
