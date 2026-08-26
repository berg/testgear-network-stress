# Findings

What this suite has turned up, and — as importantly — what looked like a
finding and was not. Each entry says which side it belongs to, because
"pyvisa-py does X" and "the server does X" are different claims and only the
first is a bug report against a client.

Reproduce anything here with the mock server; none of it needs a bench.

---

## Client-side

### A stalled VXI-11 connection is reported as VI_ERROR_IO, ~11s late

**Status:** open. Reproduced on `network-robustness` (`766d7de`) and on
upstream `main` (`1f53786`), so it is not a regression from that branch.

The fault injector stops forwarding server→client bytes mid-reply, leaving the
socket open. Nothing is closed, so there is no error for the client to notice;
only a correctly-computed deadline can end the read.

Two things are wrong with what comes back:

| client timeout | error raised | time to raise |
| --- | --- | --- |
| 1000 ms | `VI_ERROR_IO` | 12.05 s |
| 3000 ms | `VI_ERROR_IO` | 14.09 s |

- **The wrong error.** VPP-4.3 3.2.2 makes this `VI_ERROR_TSK_TIMEOUT`. A
  caller that retries on timeout and gives up on I/O errors — a reasonable and
  common policy — will give up on a link that was merely slow.
- **A fixed ~11 s that is nobody's timeout.** The client's own timeout *is*
  being honoured on top of it: +2000 ms of timeout produced +2.04 s of delay.
  But the constant underneath belongs to no configured value, so a caller
  asking for a 1 s timeout waits twelve.

The 1:1 scaling on top of a fixed floor is the signature of two deadlines in
series rather than one deadline applied to the operation. HiSLIP does not show
it: the same injection over HiSLIP raises promptly.

Reproduce:

```bash
./.venv/bin/python checks/conformance.py --protocol vxi11
# "a stalled connection times out rather than hanging"
```

### VXI-11 accepts a trigger protocol it does not implement

**Status:** open, minor. Present on `network-robustness` (`766d7de`) and
upstream `main` (`1f53786`).

`viAssertTrigger` with a protocol other than `VI_TRIG_PROT_DEFAULT` returns
`VI_SUCCESS` on a VXI-11 session. VXI-11 `device_trigger` (B.6.9) carries no
protocol selector at all, so nothing but the default can actually have been
performed. The HiSLIP session gets this right and returns
`VI_ERROR_NSUP_OPER`.

Minor, but the shape is the bad one: the caller is told the thing it asked for
happened, when what happened was something else. A silent substitution is
worse than a refusal precisely because there is nothing to notice.

Reproduce:

```bash
./.venv/bin/python checks/01_smoke.py --protocol vxi11
# "a non-default trigger protocol is refused cleanly"
```

### viFlush raises NotImplementedError out of a VXI-11 session

**Status:** open. Both trees.

`viFlush` on a VXI-11 session raises a bare Python `NotImplementedError` from
`sessions.py`. An operation a backend does not implement is supposed to answer
`VI_ERROR_NSUP_OPER`; a Python exception crossing the VISA boundary is a
contract break independent of whether flush itself is implemented, because a
caller has no reason to be catching it. In this suite it killed the run and
took the remaining 30 checks with it until the harness was taught to trap it.

The HiSLIP session implements flush and returns a status.

### VI_ATTR_IO_PROT is not readable on a VXI-11 session

**Status:** open, minor. Both trees.

`VI_ATTR_IO_PROT` reads back on a HiSLIP session and answers
`VI_ERROR_NSUP_ATTR` on a VXI-11 one, though VPP-4.3 defines it for INSTR
resources generally.

---

## Server-side

These belong to ugpibd's servers, not to any VISA client. They are recorded
because a client suite keeps bumping into them, and each one looks like a
client bug the first time.

### HiSLIP status bytes do not reflect a forced device status

VXI-11 `device_readstb` maps onto a real serial poll, so a status byte forced
at the simulated device reaches the client. HiSLIP does not poll on every
status query: the server synthesises the byte from its own view of pending
output and leaves the rest to the SRQ forwarder. A status set at the device
therefore never reaches a HiSLIP client at all.

Consequence for this suite: the `read_stb` check is VXI-11 only, and raising a
service request over HiSLIP has to go through the SRQ path rather than by
forcing a status byte.

### An empty bus read is a timeout on VXI-11 and an I/O error on HiSLIP

An instrument addressed to talk with an empty output queue reports, at the
adapter, "no data and no END". VXI-11 maps that onto its own timeout, which is
what reaches the client. ugpibd's HiSLIP server makes the opposite call
deliberately (`hislip/instrument.rs`): it answers with an error, on the grounds
that a caller cannot tell a plausible empty string from a real one, and "a
plausible lie is worse than a loud failure".

Both are defensible; they are just different, and a check that asserts a
timeout for this condition is asserting on the server's choice rather than on
the client.

---

## Not findings

### Concurrent unlocked VXI-11 queries to one instrument starve

Four sessions querying the same instrument concurrently, with no lock: over
HiSLIP all succeed; over VXI-11 they time out. That looks damning for VXI-11
until you see why.

HiSLIP's server holds the bus across the write and the read of a single query,
because the protocol pushes replies and gives the server no explicit read
request to honour. VXI-11 carries `device_write` and `device_read` as separate
RPCs and deliberately does *not* fuse them — the split is the honest mapping
for a protocol that has a real read request, and ugpibd documents it as such.

So two unlocked VXI-11 sessions interleave: both writes land, the first read
drains the whole output queue, and the second read finds nothing and waits out
its timeout. That is what a real GPIB bus does with two controllers and no
lock. The same test with `viLock` held across each query passes in 0.03 s.

The suite now asserts the locked case on both transports and the unlocked case
only on HiSLIP, where the server's bus tenure actually provides the guarantee.

### VXI-11 refusing unaddressed REN operations, and shared-lock keys

Both looked like gaps in pyvisa-py's VXI-11 session and are neither.

VXI-11 carries only *addressed* remote/local operations: `device_remote`
(B.6.13) asserts REN and addresses the device, `device_local` (B.6.14) sends
GTL. There is no RPC for driving the REN line on its own, so refusing
`VI_GPIB_REN_ASSERT` and friends with `VI_ERROR_NSUP_OPER` is conforming.
Requiring success from every `RENLineOperation` was the check being wrong.

Likewise, VXI-11 locks are exclusive, per-link and non-nesting (RULE B.6.72).
The protocol has no shared-lock concept and no field to carry a key, so a
shared lock coming back with an empty key is not a backend that lost it.

The suite now expects the addressed modes to succeed and the unaddressed ones
to be refused, and treats the shared-lock key as a HiSLIP-only assertion.

### Three harness bugs that presented as client transport bugs

Recorded because the shape recurs, and every one of them was investigated as a
finding first:

1. The fault context manager cleared knobs by setting them to `null`, which
   the control protocol reads as "leave unchanged". One check enabled
   one-byte-per-segment forwarding and every check after it timed out.
2. One layer down, serde collapses an absent field and an explicit `null` for
   `Option<Option<T>>`, so "clear this fault" arrived indistinguishable from
   "do not touch it" — same symptom, different cause, found only after fixing
   the first.
3. The virtual instrument slept for the full timeout on an empty read *while
   holding the bus mutex*, blocking every other session. A real adapter
   reports a bus timeout by returning immediately with no data and no END; the
   servers enforce the client's deadline themselves, in slices.

A harness that leaks state produces findings that point anywhere but at the
harness.
