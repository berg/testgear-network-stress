# Spec gaps

**Current state:** 139 of 854 normative statements are cited by a check, up
from 33 when this suite was first pointed at the specs. Of the remainder, 147
are client-testable and untouched -- that is the real queue, and
`tools/spec_rules.py` prints the triage that produces the number. The rest bind
other interfaces, bind the instrument server, or state no single observable
behaviour.

## Done

| area | clauses | file |
| --- | --- | --- |
| Lock semantics | VPP-4.3 3.6.10-3.6.32 | `checks/10_lock_semantics.py` |
| HiSLIP client requirements | IVI-6.1 2.3, 2.7, 3.1.2 | `checks/11_hislip_messages.py` |
| Resource template | VPP-4.3 3.2, 3.3, 3.4 | `checks/12_session_lifecycle.py` |
| Event handling | VPP-4.3 3.7 | `checks/13_events.py` |
| VXI-11 operation flags | VXI-11 B.5.3, B.5.4, B.6.14, B.6.22 | `checks/14_vxi11_flags.py` |
| Required attributes | VPP-4.3 5.1.11-5.1.54 | `checks/15_required_attributes.py` |
| Required operations, read statuses | VPP-4.3 5.1.72, 6.1.4, 6.1.5 | `checks/16_operations.py` |
| Resource name parsing | VPP-4.3 4.3.4-4.3.20, 6.2.3 | `checks/17_resource_names.py` |

Building the HiSLIP message-level injector was the enabling piece: IVI-6.1 was
0 of 103 before it, because none of section 3.1.2 is visible through the VISA
API.

## Next, in rough order of value

1. **Confirm the pending findings against a vendor.** Everything found since
   the build host went off DNS is a hypothesis. The rule this suite learned the
   hard way is that a check only pyvisa-py fails is not yet a claim about
   pyvisa-py -- and the one property that has predicted survival is whether the
   check cites a clause, which these all do.
2. **IVI-6.1 3.1.2 rules 3 and 4** -- clearing validated buffers when the
   client sends, and the Interrupted / AsyncInterrupted ordering. Both need the
   injector to *originate* messages rather than only rewrite them.
3. **IVI-6.1 3.2.2**, overlap mode client requirements. Untouched, and the
   AsyncStatusQuery MessageID rule is the same shape as the rules already
   covered for synchronised mode.
4. **VPP-4.3 6.3**, the remaining operation definitions -- 49 uncovered, the
   largest single block left.
5. **A server that chunks its replies.** Rule 2 of 3.1.2 is unreachable while
   the mock answers every read with a single DataEND, however large.

## Original assessment

Kept because the reasoning still holds and the priorities it set turned out to
be right.

## 1. Lock semantics (VPP-4.3 3.6.x) &mdash; **done**, see `checks/10_lock_semantics.py`

Twelve checks written, five failures against pyvisa-py, all cited. The nesting
completion codes (3.6.28 / 3.6.29 / 3.6.32) and the wrong-key refusal (3.6.31)
are unimplemented, and over VXI-11 a nested lock deadlocks the session against
itself. Both vendors implement all of it. Details in
[`findings.md`](findings.md).

Still unwritten from this section: 3.6.12 (shared requested while holding
exclusive), 3.6.21's shared-count half, 3.6.22's *at least* wording under
contention from more than two sessions.

### Original assessment

The richest seam, and the one where the three implementations already visibly
disagree. VPP-4.3 specifies lock behaviour in unusual detail, and almost none of
it is currently checked.

| clause | requirement | proposed check |
| --- | --- | --- |
| 3.6.10 / 3.6.11 | an acquired lock increments that session's lock count | nested `viLock` calls, then the matching number of `viUnlock` calls, is the resource still locked between? |
| 3.6.12 | a shared lock requested from a session already holding an exclusive lock | take exclusive, then request shared on the same session |
| 3.6.13 | `VI_EXCLUSIVE_LOCK` ignores `requestedKey` | pass a key with an exclusive request; it must not be honoured |
| 3.6.14 | exclusive lock sets `accessKey` to a zero-length string | already observed: NI and R&S both return a key here |
| 3.6.17 | a `requestedKey` of 256 characters or more | pass an over-long key, expect a clean error not a truncation |
| 3.6.20 | re-locking shared from the same session returns the *same* key | lock shared twice, compare keys |
| 3.6.21 | closing a session releases its locks, both counts to zero | lock, close without unlocking, confirm another session can lock |
| 3.6.22 | `viLock` waits at least the timeout before erroring | partially covered; the *at least* half is not |
| 3.6.23 | `VI_TMO_IMMEDIATE` returns immediately | contended lock with a zero timeout, assert it returns fast |

Nesting (3.6.10/3.6.11/3.6.20) is the most promising: it is real client-side
bookkeeping, invisible on the wire, and easy to get wrong.

## 2. HiSLIP MessageID validation (IVI-6.1 3.1.2) &mdash; **injection built**, see `checks/11_hislip_messages.py`

The proxy now parses HiSLIP framing in both directions: it records every message
header, and can skew the Message Parameter of a server Data or DataEND. Six
checks written. pyvisa-py passes all of the reachable ones -- the MessageID
counter starts at 0xFFFFFF00 and steps by two, and a DataEND carrying the wrong
MessageID is discarded and the session recovers.

Rule 2 (a mis-addressed *Data* message) **cannot be reached against this
server**, which sends every reply as a single DataEND even at 400 kB. The check
establishes that before claiming anything, having first been written without
that guard: it armed a fault that could never fire and reported the resulting
success as a client failure. A check bug wearing a finding's clothes, and the
second of its kind in this file's history.

Still unwritten from 3.1.2: rule 3 (sending Data clears validated buffers) and
rule 4 (Interrupted / AsyncInterrupted ordering), both of which need the
injector to originate messages rather than only rewrite them.

### Original assessment

Section 3.1.2, *Synchronized Mode Client Requirements*, is a list of things a
HiSLIP client SHALL do, and **none of it is tested by anything here**. The
central one: when a received `DataEND`'s MessageID does not match the request's,
the client shall clear any buffered Data responses and discard the message.

That is a real correctness requirement about desynchronisation recovery, and it
is exactly the failure this whole suite exists to catch. It cannot be reached
today: the fault injector understands ONC RPC records but not HiSLIP framing, so
there is no way to corrupt a MessageID.

Building HiSLIP message-level injection is the single highest-value piece of
work on this list. It would also unlock 2.7 (a message too large for the
receiver shall provoke an Error message) and the rest of 3.1.2.

## 3. VXI-11 client obligations &mdash; B.6.77 **done**

B.6.77 (locks tied to the core channel) now has a check and pyvisa-py passes it.
B.4.4 is a second citation for the stalled-connection finding.

### Original assessment

Most of VXI-11's rules bind the *server*. Six bind the client:

| clause | requirement | status |
| --- | --- | --- |
| B.4.4 | the client SHALL provide a local timeout mechanism for a server that does not respond | **this is the stalled-connection finding.** It now has a VXI-11 citation as well as VPP-4.3 3.2.2 |
| B.6.77 | locks are tied to the core connection; if it drops, they release | untested, and testable with the existing drop fault |
| B.2.6 / B.2.10 | interrupt channel establishment and teardown | partly exercised by the SRQ suite, not asserted |
| B.6.93 | `device_enable_srq` semantics | server-side |
| B.6.99 | byte-order swapping | server-side |

B.6.77 is worth doing next: take a lock, drop the connection with the existing
`drop_after_bytes` knob, and confirm another session can then acquire it.

## 4. Session and attribute lifecycle (VPP-4.3 3.2.x, 3.3.x)

Cheap, mechanical, and unglamorous:

- 3.2.5 / 3.2.6 &mdash; `VI_ATTR_MAX_QUEUE_LENGTH` is writable until the first
  `viEnableEvent` on that session and read-only after. A clean state-dependent
  attribute rule that nothing checks.
- 3.3.2 &mdash; `viClose(VI_NULL)` returns `VI_WARN_NULL_OBJECT`.
- 3.2.3 &mdash; `VI_ATTR_RSRC_SPEC_VERSION` has a fixed value.

## What cannot be tested from here

Worth writing down so it is not repeatedly rediscovered:

- Anything binding the **server** (most of VXI-11 appendix B). This suite tests
  clients; the server side would need the checks pointed the other way.
- **Local lockout** (`VI_GPIB_REN_ASSERT_LLO` and friends): LLO disables a front
  panel key, which no amount of bus traffic reveals. `reproducers/bench/`
  has the interactive script for a human at the instrument.
- Anything about **other interfaces** &mdash; 71 rules about GPIB, USB, PXI,
  serial and the VXI backplane.
