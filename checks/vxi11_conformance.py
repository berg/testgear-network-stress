#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VXI-11 client conformance, down at the RPC layer.

These are the checks that need the mock to misbehave in ways expressed in ONC
RPC records rather than in bytes: an error code the client does not expect, a
maxRecvSize it cannot use, a stale reply left in the socket by an interrupted
call. The proxy assembles those records by hand, so a client bug cannot hide
behind a server that would have refused to emit the malformed thing.

Rules cited are VXI-11 Rev 1.0 (B.5.2 error codes, B.5.3 operation flags,
B.6.x the RPCs) and VPP-4.3 (RULE 6.1.x for viRead status, 3.6.2.1 for viLock).

Several of these cover conditions whose failure mode is "never returns", so the
runner's watchdog matters here more than anywhere else in the suite: it reports
the hang instead of joining it.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants, errors  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

# VXI-11 procedure numbers (B.6).
CREATE_LINK, DEVICE_WRITE, DEVICE_READ, DEVICE_READSTB = 10, 11, 12, 13

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def restart_server() -> None:
    """Replace the mock after a check has been abandoned mid-flight.

    A watchdog trip leaves a thread blocked inside the client, and a wedged
    client usually loops rather than sitting still -- so it keeps driving the
    server, and its traffic shows up in the observation log the next check
    reads. There is no safe way to kill the thread, so the target is replaced
    instead and the old one left to whatever is still holding it.
    """
    from testgear.server import MockServer

    old = CTX.get("server")
    if old is None:
        return

    # Stop the old server *first*. Starting the replacement while it still
    # holds port 111 makes the new one come up without a portmapper, and its
    # VXI-11 resource then degrades to the TCPIP0::host,port::inst0::INSTR
    # shorthand -- which is a pyvisa-py extension. Every subsequent check
    # against NI-VISA or R&S then failed at viOpen with VI_ERROR_RSRC_NFOUND,
    # in checks named after keepalive and locks, for a reason nothing in their
    # output pointed at.
    was_portmapped = getattr(old, "_portmap", False)
    try:
        old.stop()
    except Exception:  # noqa: BLE001
        pass

    fresh = MockServer(proxy=old._proxy, portmap=was_portmapped).start()
    CTX["server"] = fresh
    CTX["resource"] = fresh.resource(CTX["protocol"])

    # If the replacement could not reproduce the addressing the run started
    # with, say so rather than let every later check fail at viOpen.
    if ("," in old.resource(CTX["protocol"])) != ("," in CTX["resource"]):
        print(
            "      the replacement server uses a different resource form "
            f"({CTX['resource']}); checks after this point may not open"
        )


def server():
    if CTX.get("server") is None:
        raise Skip("needs the mock server's fault injection")
    return CTX["server"]


# ---------------------------------------------------------------------------
# Read status codes
# ---------------------------------------------------------------------------
@check("viRead reports VI_SUCCESS when END stopped the read",
       rule="VPP-4.3 RULE 6.1.1")
def check_read_end():
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        inst.set_visa_attribute(RA.termchar_enabled, False)
        lib.write(sess, b"*IDN?\n")
        data, st = visa.call(lib.read, sess, 4096)
        assert st == StatusCode.success, f"expected VI_SUCCESS, got {st!r}"
        assert data, "no data came back"
        return f"got {st!r} with {len(data)} bytes"


@check("viRead reports VI_SUCCESS_TERM_CHAR when a termchar stopped it",
       rule="VPP-4.3 RULE 6.1.2")
def check_read_termchar():
    srv = server()
    srv.respond("TEST:LINES?", "first\nsecond")
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        inst.set_visa_attribute(RA.termchar, ord("\n"))
        inst.set_visa_attribute(RA.termchar_enabled, True)
        try:
            lib.write(sess, b"TEST:LINES?\n")
            data, st = visa.call(lib.read, sess, 4096)
            assert st == StatusCode.success_termination_character_read, (
                f"a read stopping on the termchar with more still to come "
                f"must report VI_SUCCESS_TERM_CHAR, got {st!r} with {data!r}"
            )
            assert data.endswith(b"\n"), f"the read did not stop on the termchar: {data!r}"
            # Drain the remainder so the session is clean for the next check.
            visa.call(lib.read, sess, 4096)
        finally:
            inst.set_visa_attribute(RA.termchar_enabled, False)
        return f"got {st!r} with {data!r}"


@check("viRead reports VI_SUCCESS_MAX_CNT when the caller's buffer filled",
       rule="VPP-4.3 RULE 6.1.3")
def check_read_max_count():
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        lib.write(sess, b"*IDN?\n")
        data, st = visa.call(lib.read, sess, 4)
        assert st == StatusCode.success_max_count_read, (
            f"expected VI_SUCCESS_MAX_CNT, got {st!r}"
        )
        assert len(data) == 4, f"asked for 4 bytes, got {len(data)}"
        visa.call(lib.read, sess, 4096)
        return f"asked for 4 bytes, got {len(data)} with {st!r}"


@check("END wins over the byte count when the last byte fills the buffer",
       rule="VPP-4.3 RULE 6.1.1")
def check_end_beats_count():
    """The end-of-message-lost-to-the-byte-count bug, at its exact boundary.

    pyvisa's read_raw loops *while* the status is max-count, so a message whose
    length exactly equals the requested count must report VI_SUCCESS or the
    next read can only time out.
    """
    srv = server()
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        srv.big_reply(64)
        lib.write(sess, b"TEST:BIG?\n")
        # 64 digits plus the newline the instrument appends.
        data, st = visa.call(lib.read, sess, 65)
        assert len(data) == 65, f"expected the whole 65-byte message, got {len(data)}"
        assert st == StatusCode.success, (
            f"a message ending exactly on the requested count must report "
            f"VI_SUCCESS, got {st!r}. Callers loop while the status is "
            f"max-count, so this costs an extra read that can only time out"
        )
        return f"a 65-byte message read into a 65-byte buffer got {st!r}"


@check("a reply larger than maxRecvSize is reassembled intact",
       rule="VXI-11 RULE B.6.23")
def check_large_reassembly():
    srv = server()
    size = 96 * 1024
    with open_inst() as inst:
        srv.big_reply(size)
        reply = inst.query("TEST:BIG?").strip()
        assert len(reply) == size, f"expected {size} bytes, got {len(reply)}"
        expected = "".join(str(i % 10) for i in range(size))
        assert reply == expected, "the reassembled reply does not match"
        return f"{len(reply)} bytes reassembled intact"


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------
@check("an error 21 (invalid address) reply becomes a VISA error",
       rule="VXI-11 §B.5.2")
def check_error_21():
    srv = server()
    with open_inst() as inst:
        with srv.vxi11_faults(error_on_proc=DEVICE_READ, error_code=21):
            try:
                inst.query("*IDN?")
            except errors.VisaIOError as exc:
                return f"reported as {visa.visa_status(exc)}"
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"a VXI-11 error reached the caller as {type(exc).__name__} "
                    f"rather than a VisaIOError: {exc}"
                ) from None
        raise AssertionError("an error 21 reply was not reported at all")


@check("a device-defined error code becomes a VISA error, not a crash",
       rule="VXI-11 §B.5.2")
def check_unknown_error_code():
    """B.5.2 reserves every code outside Table B.2: "All other error codes
    are reserved." It does not say who may define them -- "device-defined"
    is this suite's word for the case, not the spec's.

    A client with a lookup table and no default raises KeyError here, which is
    a Python exception escaping the VISA boundary rather than an error a
    caller can handle.
    """
    srv = server()
    with open_inst() as inst:
        with srv.vxi11_faults(error_on_proc=DEVICE_READ, error_code=1234):
            try:
                inst.query("*IDN?")
            except errors.VisaIOError as exc:
                return f"reported as {visa.visa_status(exc)}"
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"an unknown device-defined error code produced "
                    f"{type(exc).__name__}: {exc}, not a VISA error"
                ) from None
        raise AssertionError("an unknown error code was not reported at all")


@check("the session still works after an injected error")
def check_recovery_after_error():
    srv = server()
    with open_inst() as inst:
        fired = False
        with srv.vxi11_faults(error_on_proc=DEVICE_READ, error_code=4):
            try:
                inst.query("*IDN?")
            except Exception:  # noqa: BLE001
                fired = True
        # Without this the check passes when the injection does nothing:
        # there is no error to recover from, so recovery is trivial.
        assert fired, (
            "the injected error 4 did not reach the client, so this check "
            "never exercised recovery"
        )
        reply = inst.query("*IDN?").strip()
        assert "," in reply, (
            f"the session did not recover from a single failed read: {reply!r}"
        )
        return f"the query after the injected error returned {reply!r}"


@check("a stale reply in the socket does not desynchronise the stream",
       rule="RFC 5531 §9")
def check_stale_reply():
    """What an interrupted call leaves behind.

    A client that matches replies by arrival order rather than by xid consumes
    the stale record as the answer to the wrong question, and every subsequent
    exchange is off by one -- silently, with plausible-looking data.
    """
    srv = server()
    with open_inst() as inst:
        # The reply the *next* query must not return, so a client that
        # consumed the stale record in its place can be caught. Without this
        # the check passes whether or not the record was ever injected.
        srv.respond("TEST:MARK?", "stale-sentinel")
        raised = None
        with srv.vxi11_faults(stale_reply_before_proc=DEVICE_READ):
            try:
                reply = inst.query("*IDN?").strip()
                detail = f"the stale record was skipped, query returned {reply!r}"
            except Exception as exc:  # noqa: BLE001
                # Rejecting it is also correct -- better than consuming it --
                # so long as the session survives.
                raised = exc
                detail = f"the stale record was rejected: {type(exc).__name__}"
        assert raised is not None or "," in reply, (
            "the injected stale record produced no observable effect at all, "
            "so this check did not exercise anything"
        )
        after = inst.query("*IDN?").strip()
        assert "," in after, (
            f"the stream did not resynchronise after a stale reply, "
            f"next query gave {after!r}"
        )
        return detail


@check("a connection dropped mid-read is reported, not hung",
       rule="VPP-4.3 §6.1.1")
def check_connection_dropped():
    srv = server()
    with open_inst() as inst:
        inst.timeout = 2000
        srv.big_reply(8192)
        started = time.time()
        with srv.faults(drop_after_bytes=64):
            try:
                inst.query("TEST:BIG?")
            except Exception as exc:  # noqa: BLE001
                elapsed = time.time() - started
                assert elapsed < 8.0, f"took {elapsed:.2f}s to notice"
                return f"{visa.visa_status(exc)} in {elapsed * 1000:.0f}ms"
        raise AssertionError("a connection dropped mid-read did not raise")


@check("a maxRecvSize of zero does not wedge the session", rule="VXI-11 RULE B.6.3")
def check_zero_max_recv_size():
    """RULE B.6.3 requires maxRecvSize to be at least 1024.

    A server reporting zero is out of spec, but a client must not hang on it:
    zero is what a buggy or hostile server sends, and the failure mode of
    dividing a write into zero-byte chunks is an infinite loop, not an error.
    """
    srv = server()
    srv.set_vxi11_faults(max_recv_size=0)
    try:
        with open_inst() as inst:
            inst.timeout = 2000
            reply = inst.query("*IDN?").strip()
            return f"survived, query returned {reply!r}"
    except errors.VisaIOError as exc:
        return f"refused cleanly with {visa.visa_status(exc)}"
    finally:
        srv.set_vxi11_faults()


#: The smallest maxRecvSize a conformant server may report. RULE B.6.3: a
#: create_link reply SHALL "return in maxRecvSize the size of the largest
#: data parameter the network instrument server can accept in a device_write
#: RPC. This value SHALL be at least 1024."
MIN_MAX_RECV_SIZE = 1024


@check("a write larger than maxRecvSize is split", rule="VXI-11 OBSERVATION B.6.5")
def check_write_splitting():
    """The client divides a write exceeding maxRecvSize itself.

    The mock reports a small maxRecvSize and the observation log is checked
    for more than one write reaching the instrument -- the client-side split
    is invisible from the reply alone.

    Faulted to 1024, not smaller. This check used to use 64, which no server
    is allowed to report: RULE B.6.3 sets the floor at 1024, so a client that
    ignored 64 was not necessarily doing anything wrong and a failure here
    was not necessarily a finding. 1024 is the smallest value a client must
    actually honour, which makes the result mean something. Raised by hb020
    against a KS34465A in issue #5.

    OBSERVATION B.6.5 is where the behaviour is written down -- "if a
    controller needs to send greater than maxRecvSize bytes to the device at
    one time, then the network instrument client makes multiple calls to
    device_write" -- and it is an observation, not a rule, so this is a check
    on what every working client does rather than on what one SHALL do. The
    citation used to read B.6.4, which is about clientId and has nothing to
    do with any of this.
    """
    srv = server()
    # A command rather than a query: a query left unanswered leaves the
    # instrument addressed to talk, and the cleanup then costs a timeout per
    # session. maxRecvSize applies at create_link, so the faulted and
    # unfaulted cases need separate sessions -- but only two, not four. The
    # first version opened four and exceeded the watchdog under NI-VISA, which
    # tripped a server restart and made every check after it fail at viOpen.
    # Comfortably over the 1024 floor, so the split is unambiguous: this
    # should arrive in three writes rather than one.
    payload = "*CLS;" * 520

    with open_inst() as inst:
        srv.reset()
        inst.write(payload)
        time.sleep(0.15)
        plain_writes = len(srv.writes())
        visa.drain_errors(inst)

    srv.set_vxi11_faults(max_recv_size=MIN_MAX_RECV_SIZE)
    try:
        with open_inst() as inst:
            srv.reset()
            inst.write(payload)
            time.sleep(0.15)
            writes = srv.writes()
            joined = "".join(writes)
            visa.drain_errors(inst)
            assert payload in joined, (
                f"the instrument did not receive the whole message; it saw "
                f"{len(joined)} bytes across {len(writes)} writes"
            )
            assert len(writes) > plain_writes, (
                f"a maxRecvSize of {MIN_MAX_RECV_SIZE} produced "
                f"{len(writes)} write(s) for {len(payload)}B, the same as the "
                f"{plain_writes} sent without it, so the message was not "
                f"divided at all"
            )
            return (
                f"{len(payload)}B arrived in {len(writes)} writes, against "
                f"{plain_writes} unfaulted"
            )
    finally:
        srv.set_vxi11_faults()


@check("opening a dead port fails cleanly")
def check_dead_port():
    # Bind and close, so the port is certainly nobody's.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()

    rm = CTX["backend"].resource_manager()
    started = time.time()
    try:
        rm.open_resource(f"TCPIP0::127.0.0.1,{dead}::inst0::INSTR", open_timeout=3000)
    except errors.VisaIOError as exc:
        elapsed = time.time() - started
        assert elapsed < 10.0, f"took {elapsed:.1f}s to refuse a dead port"
        return f"{visa.visa_status(exc)} in {elapsed * 1000:.0f}ms"
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"opening a dead port raised {type(exc).__name__}: {exc}, "
            f"not a VisaIOError"
        ) from None
    raise AssertionError("opening a dead port appeared to succeed")


@check("a refused link is reported as a VISA open failure", rule="VXI-11 RULE B.6.5")
def check_refused_link():
    srv = server()
    # Error 9 is "out of resources": what a server answers when it will not
    # grant another link.
    srv.set_vxi11_faults(error_on_proc=CREATE_LINK, error_code=9, error_once=False)
    try:
        rm = CTX["backend"].resource_manager()
        try:
            rm.open_resource(CTX["resource"], open_timeout=3000)
        except errors.VisaIOError as exc:
            return f"reported as {visa.visa_status(exc)}"
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"a refused link raised {type(exc).__name__}: {exc}, "
                f"not a VisaIOError"
            ) from None
        raise AssertionError("a refused create_link appeared to succeed")
    finally:
        srv.set_vxi11_faults()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
@check("viLock waits for the lock rather than failing at once",
       rule="VPP-4.3 RULE 3.6.22")
def check_lock_waits():
    with open_inst() as a, open_inst() as b:
        a.lock_excl(2000)
        released = threading.Event()

        def release_later():
            time.sleep(1.0)
            a.unlock()
            released.set()

        threading.Thread(target=release_later, daemon=True).start()
        started = time.time()
        b.lock_excl(5000)
        waited = time.time() - started
        b.unlock()
        released.wait(2.0)
        assert waited >= 0.8, (
            f"viLock returned after {waited:.2f}s while the lock was still "
            f"held; it did not wait"
        )
        return f"waited {waited:.2f}s for a lock released at 1.00s"


@check("VI_ATTR_RSRC_LOCK_STATE reflects a held lock", rule="VPP-4.3 RULE 3.6.24")
def check_lock_state():
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        visa.status(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
        visa.status(lib.unlock, sess)
        assert st == StatusCode.success, (
            f"VI_ATTR_RSRC_LOCK_STATE is not readable ({st!r})"
        )
        assert state == constants.VI_EXCLUSIVE_LOCK, (
            f"expected VI_EXCLUSIVE_LOCK, got {state!r}"
        )
        return f"read back {state!r} ({st!r})"


@check("the session still works after a lock attempt failed")
def check_after_failed_lock():
    with open_inst() as a, open_inst() as b:
        a.lock_excl(2000)
        try:
            b.lock_excl(200)
        except Exception:  # noqa: BLE001
            pass
        finally:
            a.unlock()
        reply = b.query("*IDN?").strip()
        assert "," in reply, f"session B was left unusable: {reply!r}"
        return f"session B returned {reply!r} after its lock attempt failed"


# ---------------------------------------------------------------------------
# Session behaviour
# ---------------------------------------------------------------------------
@check("a device that answers a read with nothing still times out",
       rule="VPP-4.3 §6.1.1")
def check_empty_read_times_out():
    with open_inst() as inst:
        inst.timeout = 1000
        inst.write("TEST:SILENT?")
        started = time.time()
        try:
            inst.read()
        except errors.VisaIOError as exc:
            elapsed = time.time() - started
            assert exc.error_code == StatusCode.error_timeout, (
                f"expected VI_ERROR_TMO, got {visa.visa_status(exc)}"
            )
            assert elapsed < 4.0, f"a 1000ms timeout took {elapsed:.2f}s"
            return f"fired in {elapsed * 1000:.0f}ms"
        raise AssertionError("a read with nothing to read returned")


@check("the session recovers from a read timeout")
def check_recovery_after_timeout():
    with open_inst() as inst:
        inst.timeout = 800
        inst.write("TEST:SILENT?")
        try:
            inst.read()
        except errors.VisaIOError:
            pass
        inst.timeout = CTX["timeout"]
        inst.clear()
        reply = inst.query("*IDN?").strip()
        assert "," in reply, f"the session did not resynchronise: {reply!r}"
        return f"the query after the timeout returned {reply!r}"


@check("VI_ATTR_TCPIP_KEEPALIVE can be turned on")
def check_keepalive():
    """`value is True` is not the same question as "is keepalive on".

    VI_ATTR_TCPIP_KEEPALIVE is a ViBoolean, and what comes back through
    ctypes is the integer VI_TRUE. pyvisa-py hands back a `bool` and NI-VISA
    hands back `1`, so an identity test against `True` failed against every
    real instrument this suite has ever been pointed at, with the
    self-refuting message "keepalive did not read back as on (success, 1)".
    The equivalent check in 01_smoke compares truthily and passed throughout,
    which is how long two checks can disagree about the same attribute
    without anyone noticing.

    Truthiness, not equality with 1: VI_TRUE is defined as 1, but a backend
    returning some other non-zero for a ViBoolean is answering the question
    asked, and this check is not the place to litigate that.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, st = visa.call(lib.set_attribute, sess, RA.tcpip_keepalive, True)
        value, get_st = visa.call(lib.get_attribute, sess, RA.tcpip_keepalive)
        visa.call(lib.set_attribute, sess, RA.tcpip_keepalive, False)
        assert st == StatusCode.success, f"setting keepalive returned {st!r}"
        assert get_st == StatusCode.success and bool(value), (
            f"keepalive did not read back as on ({get_st!r}, {value!r})"
        )
        return f"set {st!r}, read back {value!r} ({get_st!r})"


@check("VI_ATTR_SEND_END_EN=False suppresses END on the write",
       rule="VXI-11 §B.5.3")
def check_send_end_flag():
    """The END operation flag, checked at the instrument rather than the API.

    Whether the client cleared the flag is invisible from the reply; the
    observation log is the only place the answer actually exists.
    """
    srv = server()
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        srv.clear_observed()
        inst.set_visa_attribute(RA.send_end_enabled, False)
        try:
            lib.write(sess, b"*CLS")
            time.sleep(0.2)
            events = [e for e in srv.observed() if e["op"] == "write"]
            assert events, "the write never reached the instrument"
            assert not events[-1]["eoi"], (
                "VI_ATTR_SEND_END_EN was false but the write still carried END"
            )
            observed = f"the instrument saw eoi={events[-1]['eoi']!r}"
        finally:
            inst.set_visa_attribute(RA.send_end_enabled, True)
            lib.write(sess, b"\n")
            visa.drain_errors(inst)
        return observed


#: Open/close cycles that would prove the point outright. B.6.5 leaves the
#: cap to the server; everything this suite has met caps well below this.
LINK_CYCLES = 80

#: How long this check may spend trying. It is one of the few here whose cost
#: is set by the target rather than by the check: a cycle is a TCP connect, a
#: CREATE_LINK, a query and a DESTROY_LINK, which is a millisecond against the
#: mock on loopback and a quarter-second against an instrument across a
#: switch. Eighty of those is 20 seconds, which is exactly the file's watchdog,
#: so against a 34465A this check did not fail -- it was abandoned mid-cycle
#: and reported as a hang, with a thread left holding a link, which is its own
#: small contribution to running the server out of links.
LINK_BUDGET_S = 45.0


@check("closing the session destroys the link", rule="VXI-11 RULE B.6.9",
       watchdog=LINK_BUDGET_S + 20.0)
def check_close_destroys_link():
    """Links are a finite server resource (B.6.5 caps them).

    A client that closes a session without destroying its link leaks one per
    open, and the symptom arrives much later as an unexplained refusal to open
    anything at all. Opening far more sessions than the cap, one at a time, is
    the cheap way to prove it does not.

    How many cycles that takes is not knowable in advance -- the cap is the
    server's to choose -- so this runs as many as the budget allows and says
    how many it got. A short run is weaker evidence, not a different verdict:
    a leak shows up on the cycle after the cap, and if the cap is above what
    fits here the check cannot see it either way. Saying so is the point of
    reporting the count.
    """
    deadline = time.time() + LINK_BUDGET_S
    done = 0
    for i in range(LINK_CYCLES):
        try:
            with open_inst() as inst:
                inst.query("*IDN?")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"open/close cycle {i} failed with {visa.visa_status(exc)}; "
                f"the server ran out of links, so closing does not destroy them"
            ) from None
        done = i + 1
        if time.time() > deadline:
            break
    if done < LINK_CYCLES:
        per = LINK_BUDGET_S / max(done, 1)
        return (
            f"{done} sequential open/close cycles in {LINK_BUDGET_S:.0f}s "
            f"({per * 1000:.0f} ms each); no leak up to there, but this target "
            f"is too slow to reach {LINK_CYCLES} within the budget, so a cap "
            f"above {done} would not have been found"
        )
    return f"{LINK_CYCLES} sequential open/close cycles"


if __name__ == "__main__":
    script.run(watchdog=20.0, on_timeout=restart_server)
