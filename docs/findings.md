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

**A calibration result that reversed, and the reversal is the interesting
part.** The first vendor comparison found *no* case where pyvisa-py failed and
both vendors passed, across 115 checks -- which read as reassuring. It was
mostly an artefact. Those checks had grown out of a suite written against
pyvisa-py, so they encoded its behaviour and could not, by construction, find
much it did uniquely wrong.

Rewriting the checks from spec clauses instead of from observed behaviour
produced eight such cases immediately. That is the strongest argument this repo
has for the citation discipline: the same instrument, pointed at the same three
implementations, went from finding nothing to finding eight confirmed gaps
purely because the checks were derived from what the document says rather than
from what one implementation does.

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

### Lock nesting is not implemented (VPP-4.3 3.6.28 / 3.6.29 / 3.6.31 / 3.6.32)

**Status:** open. Confirmed against both vendors.

VPP-4.3 has VISA keep a lock count per session, and defines completion codes so
a caller can tell a full release from a partial one:

| clause | requirement | pyvisa-py | NI-VISA | R&S VISA |
| --- | --- | --- | --- | --- |
| 3.6.28 | a nested exclusive lock returns `VI_SUCCESS_NESTED_EXCLUSIVE` | plain `VI_SUCCESS` (HiSLIP) / `VI_ERROR_TSK_TIMEOUT` (VXI-11) | correct | correct |
| 3.6.32 | an unlock leaving a lock held returns `VI_SUCCESS_NESTED_EXCLUSIVE` | plain `VI_SUCCESS` | &mdash; | &mdash; |
| 3.6.29 | a nested shared lock returns `VI_SUCCESS_NESTED_SHARED` | plain `VI_SUCCESS` | &mdash; | &mdash; |
| 3.6.31 | a shared re-lock with a different key returns `VI_ERROR_INV_ACCESS_KEY` | granted | &mdash; | &mdash; |

The VXI-11 case is the worst of them. VXI-11 locks are non-nesting on the wire
(RULE B.6.72), so a conforming client keeps the count itself; pyvisa-py forwards
the second `device_lock` to a server already holding that lock *for this very
session*, and the session waits out its own timeout against itself. NI and R&S
both answer `VI_SUCCESS_NESTED_EXCLUSIVE` immediately.

The good news, and worth stating because it bounds the severity: the *resource*
does stay held. A caller nesting a locked region inside another does not give
the instrument up at the inner boundary. What is missing is the reporting, and
on VXI-11 the ability to nest at all.

### A shared-lock key of 256 characters or more is accepted (VPP-4.3 3.6.17)

**Status:** open, HiSLIP.

3.6.17 makes a `requestedKey` of 256 characters or more an error. A 300-character
key is accepted and returned intact. Accepting it is not harmless: keys are how
sessions decide whether they are sharing a lock, so a client that truncated
instead would let two sessions with different keys share a lock neither asked to
share.

### The resource class suffix is matched case-sensitively (VPP-4.3 4.3.17)

**Status:** open, vendor confirmation pending. Both transports.

4.3.17 requires `viOpen` to use a case-insensitive compare when matching
resource names, and admits no exceptions. Varying each component separately
isolates the one that does not:

| component varied | result |
| --- | --- |
| `tcpip0::` interface prefix | accepted |
| `hislip0` / `inst0` device name | accepted |
| `::instr` resource class suffix | **`VI_ERROR_INV_RSRC_NAME`** |

So `TCPIP0::10.0.0.5::hislip0::INSTR` opens and
`TCPIP0::10.0.0.5::hislip0::instr` does not, and the error names the whole
string rather than the one component at fault.

This is the most likely of these findings to be hit by an actual user. A
resource name typically arrives from a configuration file or a copy-paste, its
capitalisation comes from a human, and the failure gives no hint that case is
the problem. Two of the three components already compare case-insensitively,
which suggests a regex requiring the suffix uppercase rather than a deliberate
choice.

### viSetBuf raises instead of reporting (VPP-4.3 6.2.3)

**Status:** open, vendor confirmation pending. Both transports.

6.2.3 and 6.2.4 say that a TCPIP INSTR resource which cannot set the I/O buffer
size answers `VI_ERROR_NSUP_OPER`. `viSetBuf` raises a Python exception
instead -- the same contract break as `viFlush`, and now the third operation
found doing it. Worth treating as one defect with three instances rather than
three defects: the pattern is an unimplemented operation raising rather than
returning.

### A HiSLIP status query desynchronises when a service request races it

**Status:** open on upstream `main` (`3cc4fe9`), confirmed against both
vendors. Fixed on `network-robustness` (`766d7de`). HiSLIP only; VXI-11 is
unaffected in both trees.

`viReadSTB` over HiSLIP is an `AsyncStatusQuery` on the async channel followed
by a read of the `AsyncStatusResponse`. Upstream reads that response straight
off the socket:

```python
send_msg(self._async, "AsyncStatusQuery", self._rmt, self._message_id)
self._rmt = 0
response = AsyncStatusResponse(self._async)      # protocols/hislip.py
```

But `AsyncServiceRequest` (message type 20) is **unsolicited**: IVI-6.1 has the
server send it on that same async channel whenever the device requests service,
with no relation to what the client last asked for. So a service request
arriving between the query and its answer is not a fault condition, it is the
ordinary case -- and what the client does with it is raise:

```
RuntimeError: expected message type 'AsyncStatusResponse',
              received 'AsyncServiceRequest'
```

from the header check in `RxHeader.__init__`. Two things are wrong, in the
familiar order. The lesser is that a `RuntimeError` crosses the VISA boundary
where VPP-4.3 requires a status -- the same contract break as `viFlush` and
`viSetBuf` above, and now the fourth instance. The greater is that the session
is left desynchronised: the response the client was waiting for is still in the
channel, so every later read is off by one message.

`network-robustness` puts a reader thread on the async socket that splits
service requests out from responses, which is what the channel's shape asks
for.

Both vendors handle it, so this is a disparity rather than a reading of the
spec:

| implementation | `status queries stayed intact while SRQs fired` |
| --- | --- |
| NI-VISA 26.5.0 | passes |
| R&S VISA 5.12.9 | passes |
| PyVISA-py upstream `main` | `RuntimeError` out of `viReadSTB` |
| PyVISA-py `network-robustness` | passes |

This one was invisible until recently. The check that catches it sits after an
`enable_event(VI_EVENT_SERVICE_REQ)` that upstream answers with
`VI_ERROR_INV_EVENT` -- SRQ over HiSLIP is simply not implemented there -- and
the raise from *that* used to end the script, taking this and three other
failures with it. See "Skips are not passes" in the README, and the same
lesson at `viFlush` below: an unguarded call that aborts the run hides
everything behind it.

Reproduce:

```bash
./.venv/bin/python checks/03_srq.py --protocol hislip --pyvisa-py /path/to/upstream
# "status queries stayed intact while SRQs fired"
```

### viClear over HiSLIP raises when a reply is still unread (VPP-4.3 3.2.3)

**Status:** open on upstream `main` (`3cc4fe9`), confirmed against both
vendors. Fixed on `network-robustness` (`766d7de`). HiSLIP only; VXI-11 passes
in both trees.

Write a query, do not read the answer, then call `viClear`. Discarding exactly
that abandoned reply is what `viClear` is *for* -- and it is where upstream
breaks:

```
RuntimeError: expected message type 'DeviceClearAcknowledge',
              received 'DataEnd: bytearray(b'stale-marker\n')'
```

`device_clear_complete` sends `DeviceClearComplete` and then reads
`DeviceClearAcknowledge` off the **sync** channel -- the same channel where the
undelivered `DataEnd` from the abandoned reply is still queued. The pending
data is not drained first, so the acknowledgement read finds it and the header
check raises.

VPP-4.3 3.2.3 types `viClear` as returning a `ViStatus`, so a Python exception
is the wrong answer whatever the cause. But the cause is the more interesting
half: the one operation whose job is to clear stale data is the one that
stale data breaks, which means it can only be relied on when it was not needed.

The damage outlives the call. The session stays desynchronised, and the large
query that follows fails too:

```
RuntimeError: protocol synchronization error
```

Both vendors pass both halves of this on the same mock, over the same
transport:

| implementation | `clear discards an uncollected response` | `clear resyncs mid-message` |
| --- | --- | --- |
| NI-VISA 26.5.0 | passes | passes |
| R&S VISA 5.12.9 | passes | passes |
| PyVISA-py upstream `main` | `RuntimeError` | `RuntimeError` |
| PyVISA-py `network-robustness` | passes | passes |

Reproduce:

```bash
./.venv/bin/python checks/07_clear.py --protocol hislip --pyvisa-py /path/to/upstream
# "viClear returns a status rather than raising (unread response)"
```

### HiSLIP reconnection fails partway through open/close churn, on Windows

**Status:** open, **side not yet determined**. Reproduced on every CI run that
has exercised it -- three of three -- on `windows-latest`, over HiSLIP, against
upstream `main` (`3cc4fe9`).

`04_concurrency` ends by opening and closing the same resource twenty times,
checking that threads and descriptors are reclaimed. On Windows the HiSLIP
session stops being resolvable partway through:

```
VisaIOError: VI_ERROR_RSRC_NFOUND (-1073807343)
```

after seven of the twenty cycles, for a resource that had opened successfully
seven times immediately before and that the run had been using throughout.

**It stops at exactly seven, twice running.** The two runs that reported a
count both got 7/20, which is the most informative thing about it: a timing
race against a shared cloud runner would not land on the same number twice.
Something is exhausted rather than delayed. A HiSLIP session is two TCP
connections that the server pairs by session id, so fourteen connections into
the churn is where to look -- a session-id or listener limit on the mock's
side, or something the client is not releasing on close.

**And the mock has exactly such a limit.** `server/src/hislip/server.rs` caps
concurrent sessions at 16 and answers the seventeenth with a HiSLIP
`FatalError` of `MaximumClientsExceeded` ("too many active sessions"). Sessions
leave that registry through a `Drop` that spawns the removal, so a close whose
teardown does not complete promptly leaves the entry behind. `04_concurrency`
opens one session, then six in parallel, then churns -- which puts the count in
the right neighbourhood by the seventh cycle if the earlier ones are not being
reaped.

That makes the mock the leading suspect, and this suite brings its own
instrument precisely so that question gets asked. It is **not** yet settled:
the arithmetic is suggestive rather than exact, and confirming it needs the
server's own log, which the leg does not currently capture. Two things would
settle it -- capturing the mock's stderr from a Windows leg, and running the
same churn against a vendor VISA on the same host. If Keysight churns cleanly
where pyvisa-py does not, the limit is not the explanation.

The transport asymmetry is the useful part, because it holds *within a single
run on a single machine*:

| | Linux | Windows |
| --- | --- | --- |
| HiSLIP | 20/20 | **7/20** |
| VXI-11 | 20/20 | 20/20 |

VXI-11 churns the same resource on the same runner in the same job and does
not fail. So this is not the runner being slow or the mock being generally
unavailable -- something specific to reopening a *HiSLIP* connection on Windows
does not survive repetition.

**Which side it belongs to is exactly what is not yet known**, and this suite
brings its own instrument, so that question has to be asked before the finding
means anything. `VI_ERROR_RSRC_NFOUND` is what pyvisa-py raises when the HiSLIP
`Instrument` constructor cannot establish its two sockets, and a fast
close-then-reopen on Windows is equally consistent with the client not
releasing something between sessions and with the mock not reaping what the
client released. The second is ours, and on the evidence above it is the more
likely of the two. Deciding between them wants a run against real hardware, or
against a vendor implementation on the same Windows host -- which is what the
Keysight and TekVISA legs will give once their installers are provisioned. If
NI or Keysight churns cleanly where pyvisa-py does not, the answer is the
client; if nothing churns cleanly on Windows over HiSLIP, the answer is the
mock.

Recorded now rather than after that, because it is the kind of thing that gets
lost: it appears only in CI, only on a platform nobody here develops on, and
until the churn loop was wrapped it ended the script and reported nothing at
all -- the second sighting exists only because the loop now says how many
cycles it completed.

Reproduce (Windows):

```powershell
uv run python checks\04_concurrency.py --protocol hislip --pyvisa-py C:\path\to\upstream
# "20 open/close cycles all reopen the resource"
```

### What the vendor runs settled

Both transports have now been through NI-VISA 26.5.0 and R&S VISA 5.12.9.
**Eleven findings are confirmed over HiSLIP and eight over VXI-11** -- meaning
pyvisa-py fails and both vendors pass. The HiSLIP set is the superset:

> Two more have been confirmed against the same vendor reports since this
> section was written -- the HiSLIP status-query race and the HiSLIP `viClear`,
> both above. They are not folded into the table below because that table is
> the account of one run, and both were found against upstream `main` rather
> than the tree that run measured.

| clause | what pyvisa-py does | HiSLIP | VXI-11 |
| --- | --- | --- | --- |
| 3.2.3 | `VI_ATTR_RSRC_SPEC_VERSION` unsupported | confirmed | confirmed |
| 3.2.5 | `VI_ATTR_MAX_QUEUE_LENGTH` unsupported | confirmed | confirmed |
| 3.3.2 | `viClose(VI_NULL)` answers `VI_ERROR_INV_OBJECT` | confirmed | confirmed |
| 3.4.2 | a termination character of `0x1FF` is stored as `511` rather than masked | confirmed | confirmed |
| 3.6.17 | a 300-character shared-lock key is accepted | confirmed | n/a |
| 3.6.28 | a nested exclusive lock is not reported | confirmed | confirmed |
| 3.6.32 | the unlock leaving a lock held is not reported | confirmed | n/a |
| 3.7.6 | `viEnableEvent(VI_HNDLR)` succeeds with no handler | confirmed | confirmed |
| 3.7.13 | both callback modes at once is accepted | confirmed | confirmed |
| 4.3.17 | the resource class suffix is case-sensitive | confirmed | confirmed |
| 5.1.12 | four required message-based attributes missing | &mdash; | confirmed |
| 5.1.17 | `VI_ATTR_TCPIP_PORT`, `VI_ATTP_TCPIP_NODELAY` missing | confirmed | n/a |
| 5.1.72 | operations raise instead of returning a status | &mdash; | confirmed |

Three checks needed correcting first, all of them mine, and two were the *same
mistake made twice*: asserting plain `VI_SUCCESS` where VPP-4.3 defines a
distinct `VI_SUCCESS_NESTED_*` completion code, so NI-VISA failed a rule it
implements correctly. The third was measuring the test rig -- NI normalises the
port out of the canonical resource name, which cannot then reopen a mock
listening on an ephemeral port.

### A rule no implementation satisfies

5.1.11 requires `VI_ATTR_TRIG_ID` of every INSTR resource. pyvisa-py, NI-VISA
and R&S VISA all answer `VI_ERROR_NSUP_ATTR` for it on a TCPIP session.

Three independent implementations agreeing is evidence about the clause rather
than about the implementations: the attribute selects a hardware trigger line,
which a TCPIP session does not have, so the requirement reads as drafted for
the backplane interfaces and applied to INSTR generally. The suite records it
and would want a second look if it ever started passing.

### Findings that belong to the vendors

Recorded because the suite exists to find disparities, not to prosecute one
implementation:

- **R&S VISA**: `viWaitOnEvent` does not dequeue an event whose type was
  disabled after it arrived. 3.7.21 drains the queue regardless of enabled
  state and 3.7.23 says so for exactly this case. pyvisa-py and NI both get it
  right, and it is the rule most likely to be got wrong by an implementation
  that treats *disabled* as *empty*.
- **R&S VISA**: shared locks over HiSLIP are refused outright with
  `VI_ERROR_INV_PROTOCOL`, where both others grant them.

### What the first vendor run settled

Eight of the pending hypotheses are now confirmed findings: **pyvisa-py fails,
NI-VISA and R&S VISA both pass.**

| clause | what pyvisa-py does |
| --- | --- |
| 3.6.28 | a nested exclusive lock is not reported (over VXI-11 it deadlocks the session against itself) |
| 3.2.5 | `VI_ATTR_MAX_QUEUE_LENGTH` unsupported |
| 3.3.2 | `viClose(VI_NULL)` answers `VI_ERROR_INV_OBJECT` |
| 3.7.6 | `viEnableEvent(VI_HNDLR)` succeeds with no handler installed |
| 3.7.13 | `VI_SUSPEND_HNDLR | VI_HNDLR` accepted |
| 5.1.12 | four required message-based attributes missing |
| 5.1.72 | required operations raise instead of returning a status |
| 4.3.17 | the resource class suffix is matched case-sensitively |

Two more are confirmed by one vendor and contradicted by the other, so they are
weaker but not empty: 5.1.11's attribute list (R&S passes, NI also fails) and
`viSetBuf` (NI passes, R&S also fails).

Two checks failed on **all three**, which per this suite's own rule means
suspect the check. Both were:

- `VI_ATTR_RSRC_SPEC_VERSION` -- the value is a revision number and
  implementations track different revisions. NI answers `0x00700000` (VISA 7.0)
  against a 2024 document specifying `0x00700200` (7.2). Failing an
  implementation for being built against an earlier edition is measuring the
  calendar, so the check now asserts the attribute exists and names a version.
  pyvisa-py still fails it, because it does not implement the attribute at all.
- The 3.4.2 termination-character case -- *no* implementation refuses an
  out-of-range value, so the rule as written is not what any of them does. What
  separates them is what happens next: NI masks `0x1FF` to `0xFF`, pyvisa-py
  stores `511` and reads it back. A termination character is one byte on every
  transport here, so the attribute ends up disagreeing with anything that can go
  on the wire. The check now tests that, which is the part that matters.

### A finding about R&S VISA, not pyvisa-py

`viWaitOnEvent` on R&S does **not** dequeue an event whose type was disabled
after the event arrived. VPP-4.3 3.7.21 drains the queue regardless of enabled
state, and 3.7.23 says so explicitly for this case. pyvisa-py and NI-VISA both
get it right.

Recorded because the suite exists to find disparities rather than to prosecute
one implementation, and because it is the rule most likely to be got wrong by
an implementation that treats *disabled* as *empty* -- which is exactly what
appears to have happened here.

### Required INSTR attributes are missing (VPP-4.3 5.1.11, 5.1.12, 5.1.17)

**Status:** open, vendor confirmation pending.

VPP-4.3 5.1 lists, in plain prose, the attributes an implementation SHALL
support. They stack by specificity: every INSTR resource, then every
message-based one, then TCPIP, then HiSLIP. Reading each list back:

| clause | applies to | missing on HiSLIP | missing on VXI-11 |
| --- | --- | --- | --- |
| 5.1.11 | every INSTR resource | `VI_ATTR_TRIG_ID` | `VI_ATTR_INTF_INST_NAME`, `VI_ATTR_INTF_NUM`, `VI_ATTR_TRIG_ID`, `VI_ATTR_DMA_ALLOW_EN` |
| 5.1.12 | message-based, TCPIP named explicitly | &mdash; | `VI_ATTR_IO_PROT`, `VI_ATTR_RD_BUF_OPER_MODE`, `VI_ATTR_WR_BUF_OPER_MODE`, `VI_ATTR_FILE_APPEND_EN` |
| 5.1.16 | any TCPIP INSTR | &mdash; | &mdash; |
| 5.1.17 | HiSLIP TCPIP | `VI_ATTR_TCPIP_PORT`, `VI_ATTR_TCPIP_NODELAY` | n/a |

**This corrects something previously written down as harmless.** The HiSLIP
suite this repo grew out of recorded that `VI_ATTR_TCPIP_PORT` "is set but
unreadable on HiSLIP sessions", reasoning that "the VISA spec defines it for
SOCKET and VICP resources, not TCPIP INSTR, so the entry in `after_parsing` is
dead. Harmless."

RULE 5.1.17 says the opposite in as many words: an INSTR resource
implementation for a HiSLIP TCPIP system SHALL support `VI_ATTR_TCPIP_PORT`,
along with `VI_ATTR_TCPIP_NODELAY`. The attribute is required, the dead entry
is a real gap, and the earlier conclusion was reached by reasoning about the
spec rather than by reading it -- which is exactly the failure mode the note in
this repo about never citing a clause from memory exists to prevent.

The VXI-11 column is the larger gap, and `VI_ATTR_IO_PROT` appears in it, which
matches the separately-recorded observation that the attribute reads back over
HiSLIP and not over VXI-11. 5.1.12 makes it required on both.

### Retracted: the Data-chunk "truncation" was the check's fault, twice

This was recorded, and reported, as the most serious client-side finding here:
a mis-addressed `Data` chunk in a 500-byte reply produced 381 bytes and no
error, which was called silent truncation. **It was wrong.** All three
implementations behave identically and all three are conforming.

Two separate mistakes, and the second is the more embarrassing:

**The assertion misread the rule.** IVI-6.1 3.1.2 rule 2 says the client
"shall clear any Data responses already buffered and discard the offending Data
message". It does not say abort the read or report an error. When a
mis-addressed `Data` chunk is followed by a correctly-addressed `DataEND`, the
conforming outcome is precisely that the chunk's bytes never reach the caller
and the `DataEND` completes the message. 500 minus the 120-byte chunk the
client was required to throw away is 380. The suite's own rule caught it --
all three failing means suspect the check -- but only after the claim had been
made out loud.

**The corrected assertion could not tell the cases apart.** The rewritten
check compared the reply against the discarded chunk's bytes, using
`TEST:BIG?`, whose payload is a repeating digit pattern. The discarded chunk
and the kept remainder are byte-identical under that pattern, so the check
reported a conforming client as non-conforming for a completely different
reason. Degenerate test data will confirm whatever you already believe.

With two distinguishable halves -- 120 `A`s then 380 `B`s -- the answer is
unambiguous: 380 bytes delivered, no `A` anywhere in them. pyvisa-py, NI-VISA
and R&S VISA all discard the mis-addressed chunk exactly as the rule requires.

### New coverage this produced anyway

Making the reply chunk was still worth doing. The `Data` path had never been
exercised by anything -- ugpibd splits a reply only when it exceeds the maximum
the client declared, and pyvisa-py declares a megabyte, so every reply arrived
as a single `DataEND`. Two checks now cover it, and all three implementations
pass both:

- a reply arriving as `Data` + `DataEND` reassembles to the full 500 bytes
  (IVI-6.1 3.1.1);
- a mis-addressed `Data` chunk is dropped rather than delivered
  (IVI-6.1 3.1.2 rule 2).

### The resource template is largely unimplemented (VPP-4.3 3.2, 3.3, 3.4, 3.7)

**Status:** open, and **confirmed**. NI-VISA and R&S VISA both pass every entry
below that pyvisa-py fails. Both transports behave identically, which is itself
informative: these are template-level rules, not transport ones.

| clause | requirement | pyvisa-py |
| --- | --- | --- |
| 3.2.3 | `VI_ATTR_RSRC_SPEC_VERSION` exists and names a VISA revision | not readable |
| 3.2.5 / 3.2.6 | `VI_ATTR_MAX_QUEUE_LENGTH` is writeable until the first `viEnableEvent`, read-only after | unsupported entirely |
| 3.3.2 | `viClose(VI_NULL)` returns `VI_WARN_NULL_OBJECT` | `VI_ERROR_INV_OBJECT` |
| 3.4.2 | an out-of-range attribute state is not stored verbatim | a termination character of `0x1FF` reads back as `511` |
| 3.7.6 | `viEnableEvent(VI_HNDLR)` with no handler returns `VI_ERROR_HNDLR_NINSTALLED` | succeeds |
| 3.7.13 | `VI_SUSPEND_HNDLR | VI_HNDLR` returns `VI_ERROR_INV_MECH` | accepted |

The two event ones are the least cosmetic. Enabling the callback mechanism with
no handler leaves a session enabled for a delivery route that cannot deliver, so
events are dropped and nothing says so; and accepting both callback modes at
once leaves the caller unable to tell whether delivery is immediate or deferred.

What pyvisa-py gets right here is worth recording too, because it is the rule
most likely to be got wrong: an event queued before its type was disabled is
still dequeued (3.7.21, 3.7.23), rather than being lost by an implementation
that treats *disabled* as *empty*.

### viFlush is unimplemented on VXI-11, and raises rather than reporting

**Status:** open. Both trees. Two rules, not one.

`viFlush` on a VXI-11 session raises a bare Python `NotImplementedError` from
`sessions.py`.

**RULE 5.1.72** settles the first half: an INSTR resource implementation for a
TCPIP system SHALL support `viFlush`, among a list of twenty-three operations.
It is required, not optional, so this is a missing feature rather than a
reasonable omission -- which is how it was first recorded here, before the
clause was read.

**The second half stands on its own.** Even for an operation a backend
genuinely does not implement, the answer owed is `VI_ERROR_NSUP_OPER`. A Python
exception crossing the VISA boundary cannot be caught by a caller who had no
reason to expect it, and in this suite it killed the run and took the remaining
30 checks with it until the harness was taught to trap it.

The HiSLIP session implements flush and returns a status.

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

**RULE 6.5.6 later settled it outright**, and is worth quoting because the
conclusion above had been reached by inference: an INSTR implementation of
`viGpibControlREN` for a TCPIP system SHALL support
`VI_GPIB_REN_DEASSERT_GTL`, `VI_GPIB_REN_ASSERT_ADDRESS`,
`VI_GPIB_REN_ASSERT_ADDRESS_LLO` and `VI_GPIB_REN_ADDRESS_GTL`. Exactly the four
addressed modes, and nothing else. So refusing the unaddressed ones is
conforming by citation rather than by argument, and the suite now checks the
required four are all present -- which pyvisa-py passes on both transports.

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

## How much to trust the totals

Not much. The per-implementation counts invite a reading they cannot support,
and the bias runs one way.

**The checks were tuned until pyvisa-py passed.** This suite grew out of one
that existed to find bugs in pyvisa-py, on a branch that then fixed them. Every
check in it has been iterated against pyvisa-py's behaviour; none was iterated
against NI's. When a check failed pyvisa-py for a reason that turned out to be
the check's fault, it was relaxed -- correctly, each time, and the termchar
status code, the REN error code, the exclusive-lock key and the shared-lock key
type were all relaxed in a single afternoon. But nobody was doing that for
NI-VISA, so the ratchet only ever turned one way.

The first vendor run measured the size of that effect by accident: of 17 checks
that pyvisa-py passed and both vendors failed, 7 were checks already identified
as over-fitted and relaxed *earlier the same day* -- the run simply predated the
fix. Re-running with the corrected checks:

| implementation | failures before | failures after |
| --- | --- | --- |
| PyVISA-py | 5 | 5 |
| NI-VISA | 18 | 11 |
| R&S VISA | 26 | 19 |

Both vendors shed exactly 7, and pyvisa-py shed none. Before any triage at all,
the raw comparison overstated pyvisa-py's lead by about 40%.

**Both ends of the wire grew up together.** The mock is ugpibd's server, which
has been exercised against pyvisa-py for months. That is not the failure this
repo's README warns about -- the mock was not written alongside the checks -- but
it is a close relative: a client and a server that have only ever been pointed
at each other agree about more than the spec requires, and a third
implementation arriving late looks wrong for differences that are nobody's bug.

**What survives the discount.** Two things.

A check that cites a spec clause is worth more than one that does not, because
the clause is an authority outside the suite -- and that turns out to be a
usable filter rather than good manners. Of the original 17, exactly the 7 with
no clause cited were the over-fitted ones, and all 10 that survived the
correction cite one. Seven for seven: every uncited check in that set was the
check's fault, and none of the cited ones has been shown wrong yet.

The rule that follows is cheap to apply: **an uncited check is suspect until a
clause is found for it or it is deleted.** Not because a citation makes a check
correct, but because writing one forces the question "what says so?", which is
exactly the question an over-fitted check cannot answer.

And the direction of the strongest results is the right way round. The two
hardest checks here -- `maxRecvSize=0` and the stalled connection -- are ones
NI-VISA passes and pyvisa-py fails, both written from a clause rather than from
observed behaviour. A suite rigged to flatter pyvisa-py would not produce those,
so the instrument is not simply broken; its aggregate is.

**What would fix it.** Validate a new check against NI-VISA before trusting it,
not after. Record every relaxation where it can be seen, so the ratchet is
auditable instead of invisible. And report per-implementation rather than as a
score, since the score is the part that misleads.

---

## Not yet triaged

The VXI-11 vendor run leaves 10 checks that pyvisa-py passes and **both**
vendors fail, plus a cluster that only R&S fails (multiple sessions,
error-queue cleanliness, recovery after a timeout). None of those are claims
yet, in either direction.

All 10 cite a clause, which is why they survived the first correction pass and
why they are worth the triage:

| check | clause |
| --- | --- |
| a connection lost mid-reply is reported, not hung | VPP-4.3 3.2.2 |
| a write larger than maxRecvSize is split | VXI-11 B.6.4 |
| viLock waits for the lock rather than failing at once | VPP-4.3 3.6.2.1 |
| VI_ATTR_RSRC_LOCK_STATE reflects a held lock | VPP-4.3 3.6.2.1 |
| the session still works after a lock attempt failed | VPP-4.3 3.6.2.1 |
| a device that answers a read with nothing still times out | VPP-4.3 3.2.2 |
| the session recovers from a read timeout | VPP-4.3 3.2.2 |
| VI_ATTR_TCPIP_KEEPALIVE can be turned on | VPP-4.3 3.5 |
| VI_ATTR_SEND_END_EN=False suppresses END on the write | VXI-11 B.5.3 |
| closing the session destroys the link | VXI-11 B.6.16 |

Two readings are possible for each and they need separating one at a time:

- the check encodes pyvisa-py's behaviour rather than the spec's, and the
  vendors are right -- which is what happened with the REN error codes above;
- or the vendors genuinely differ, in which case the finding belongs to them.

A third possibility applies to at least one: `closing the session destroys the
link` fails at *cycle 0* under both vendors, with a message asserting the
server ran out of links. Failing on the very first iteration means the message
is wrong about its own cause, so that one is a check bug before it is anything
else -- a citation does not make a check right, it only makes it answerable.

Until each has been through that, they stay here rather than in the lists
above. A suite that reports 17 vendor failures it has not investigated is
making 17 claims it cannot support.
## Could the mock server be causing these?

Worth asking directly, because the mock is the one component every result
passes through, and it is derived from ugpibd rather than from a vendor
instrument. If it does something unusual, a client that is stricter than the
others would look broken.

Two things bound the risk.

**All three implementations run against the identical server.** A server quirk
applies equally to every column, so it cannot by itself produce a failure in
one column and passes in the other two. For the mock to explain a pyvisa-py-only
result it would have to do something that *only pyvisa-py* is sensitive to --
possible, but a much narrower claim than "the mock is wrong".

**Eleven of the fourteen confirmed findings involve no wire exchange whose
outcome the server could influence.** They are attribute reads and writes,
argument validation, and resource-name parsing -- decided inside the client
before or without any exchange. Two reproduce with no mock process running at
all, against an address with nothing listening:

    viClose(VI_NULL)                     -> VI_ERROR_INV_OBJECT
                                            (3.4.3 requires VI_WARN_NULL_OBJECT)
    viParseRsrc tcpip0::...::instr       -> InterfaceType.unknown
    viParseRsrc TCPIP0::...::INSTR       -> InterfaceType.tcpip
                                            (4.3.17 requires a case-insensitive compare)

The remaining three are the lock-nesting and shared-key findings, where the
server does participate. Those are the ones a second server implementation
would most usefully re-test; the vendors passing them against this same server
is evidence, but not the same quality of evidence as the eleven above.

The honest summary: the mock is a real confound for 3 of 14 findings and
effectively not a confound for the other 11.
