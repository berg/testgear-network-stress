# Findings

What this suite has turned up, and — as importantly — what looked like a
finding and was not. Each entry says which side it belongs to, because
"pyvisa-py does X" and "the server does X" are different claims and only the
first is a bug report against a client.

Reproduce anything here with the mock server; none of it needs a bench.

**Three implementations now.** Entries below carry NI-VISA 26.5.0 and R&S VISA
5.12.9 results alongside pyvisa-py, all three run from the same container on the
same kernel against the same mock (see `remote-compare.sh`). That upgrades most
of these from "disagrees with the spec" to "disagrees with a shipping
implementation", and it demoted one entry to a non-finding -- which is the
point of doing it.

One calibration result worth stating plainly: across 115 VXI-11 checks there is
**no case where pyvisa-py fails and both vendors pass**. Where pyvisa-py is
wrong it usually has company.

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

Against the same injection with a 2000 ms timeout:

| implementation | behaviour |
| --- | --- |
| NI-VISA 26.5.0 | `VI_ERROR_TSK_TIMEOUT` in 2001 ms |
| PyVISA-py | `VI_ERROR_IO`, ~11 s late |
| R&S VISA 5.12.9 | never returns at all (30 s watchdog) |

NI is exactly right: the correct error, at the configured deadline, to the
millisecond. So both halves of this -- the error code and the timing -- are
achievable, and neither is a consequence of the fault being unusual.

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

### A maxRecvSize of zero wedges a VXI-11 session indefinitely

**Status:** open. Both trees. The most serious thing in this list.

`create_link` reports the largest write the server will accept (B.6.3, which
requires at least 1024). If a server answers zero, pyvisa-py hangs: the write
path divides the message into `maxRecvSize` chunks and never terminates. It is
not slow, it does not time out, and the session timeout does not apply --
`viWrite` simply never returns. The suite's watchdog reports it after 20s;
without one it hangs the run.

| implementation | behaviour |
| --- | --- |
| NI-VISA 26.5.0 | survives, and the query returns its data |
| PyVISA-py | never returns |
| R&S VISA 5.12.9 | never returns |

NI proves this is defensible, which is the argument that matters: it is not an
inherent consequence of a server sending nonsense, it is a missing bounds
check. A server reporting zero is out of spec, but zero is exactly what a
half-initialised field or a byte-order slip produces, the client is the side
that can defend itself, and an unkillable loop inside a library call is much
worse than an error. Rejecting the value, or falling back to the 1024 floor
B.6.3 guarantees, is the whole fix.

R&S sharing the bug is worth knowing but is not a defence.

The abandoned thread keeps running afterwards and keeps driving the server,
which is its own hazard for anything sharing that server.

Reproduce:

```bash
./.venv/bin/python checks/vxi11_conformance.py
# "a maxRecvSize of zero does not wedge the session"
```

### VXI-11 interrupts are not acknowledged, throttling SRQs to one per second

**Status:** open. Both trees.

`device_intr_srq` (B.6.30) is an ONC RPC with a void reply -- void is not the
same as absent, and the server is entitled to wait for it. pyvisa-py treats the
interrupt as one-way and sends nothing back, so a server that waits pays a
timeout per service request.

Measured against ugpibd's server, which allows 1000 ms for the acknowledgement:

| transport | SRQ delivery latency |
| --- | --- |
| HiSLIP | 0.00s, 0.00s, 0.00s, 0.00s, 0.00s |
| VXI-11 | 0.00s, 1.00s, 1.00s, 1.00s, 1.00s |

The first is free and every one after it costs exactly the server's timeout,
which is the signature of a reply nobody sent rather than of load. The SRQs all
arrive and none are lost, so nothing fails -- it is purely a throughput
ceiling, and a hard one: one service request per second on a transport that
otherwise sustains thousands of operations per second.

It is worth calling this half server-side, because a server that treated the
interrupt as one-way would never notice. But the acknowledgement is the
client's to send, and any server that does wait for it is entitled to.

Reproduce: `checks/03_srq.py --protocol vxi11` takes about 60s for 30 service
requests and about 2s for the same 30 over HiSLIP.

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
`VI_GPIB_REN_ASSERT` and friends is conforming. Requiring success from every
`RENLineOperation` was the check being wrong.

The vendor run confirmed the substance and corrected the detail: all three
implementations refuse the unaddressed modes, and they disagree only about how
to say so -- pyvisa-py answers `VI_ERROR_NSUP_OPER`, NI and R&S both answer
`VI_ERROR_INVALID_MODE`. Asserting one specific code made a check that failed
two conforming implementations, so it now accepts either and records which was
used.

Likewise, VXI-11 locks are exclusive, per-link and non-nesting (RULE B.6.72).
The protocol has no shared-lock concept and no field to carry a key, so a
shared lock coming back with an empty key is not a backend that lost it.

The suite now expects the addressed modes to succeed and the unaddressed ones
to be refused, and treats the shared-lock key as a HiSLIP-only assertion.

### An access key returned for an exclusive lock

VPP-4.3 leaves `accessKey` unused for an exclusive lock, so pyvisa-py's empty
string is right. NI and R&S both hand back a generated key anyway. Nothing
depends on it being empty, and two implementations doing it means it is a
convention rather than a mistake, so the suite records it instead of failing
it.

Related and genuinely inconsistent: shared-lock keys come back as `str` from
pyvisa-py and `bytes` from NI-VISA. A caller comparing the key it passed
against the key it got back therefore has to know which backend it is on. That
is a pyvisa-level wart rather than a pyvisa-py one.

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

---

## Not yet triaged

The VXI-11 vendor run left 17 checks that pyvisa-py passes and **both** vendors
fail, plus a cluster that only R&S fails (multiple sessions, error-queue
cleanliness, recovery after a timeout). None of those are claims yet, in either
direction.

Two readings are possible for each and they need separating one at a time:

- the check encodes pyvisa-py's behaviour rather than the spec's, and the
  vendors are right -- which is what happened with the REN error codes above;
- or the vendors genuinely differ, in which case the finding belongs to them.

A third possibility applies to at least one: `closing the session destroys the
link` fails at *cycle 0* under both vendors, with a message asserting the
server ran out of links. Failing on the very first iteration means the message
is wrong about its own cause, so that one is a check bug before it is anything
else.

Until each has been through that, they stay here rather than in the lists
above. A suite that reports 17 vendor failures it has not investigated is
making 17 claims it cannot support.