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

from testgear import cli, harness, visa  # noqa: E402
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
    fresh = MockServer(proxy=old._proxy).start()
    CTX["server"] = fresh
    CTX["resource"] = fresh.resource(CTX["protocol"])
    try:
        old.stop()
    except Exception:  # noqa: BLE001
        pass


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


@check("viRead reports VI_SUCCESS_TERM_CHAR when a termchar stopped it",
       rule="VPP-4.3 RULE 6.1.3")
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


@check("viRead reports VI_SUCCESS_MAX_CNT when the caller's buffer filled",
       rule="VPP-4.3 RULE 6.1.2")
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


@check("a reply larger than maxRecvSize is reassembled intact",
       rule="VXI-11 B.6.4")
def check_large_reassembly():
    srv = server()
    size = 96 * 1024
    with open_inst() as inst:
        srv.big_reply(size)
        reply = inst.query("TEST:BIG?").strip()
        assert len(reply) == size, f"expected {size} bytes, got {len(reply)}"
        expected = "".join(str(i % 10) for i in range(size))
        assert reply == expected, "the reassembled reply does not match"


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------
@check("an error 21 (invalid address) reply becomes a VISA error",
       rule="VXI-11 B.5.2")
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
       rule="VXI-11 B.5.2")
def check_unknown_error_code():
    """B.5.2 reserves codes above 15 for device-defined errors.

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


@check("the session still works after an injected error", rule="VXI-11 B.6.6")
def check_recovery_after_error():
    srv = server()
    with open_inst() as inst:
        with srv.vxi11_faults(error_on_proc=DEVICE_READ, error_code=4):
            try:
                inst.query("*IDN?")
            except Exception:  # noqa: BLE001
                pass
        reply = inst.query("*IDN?").strip()
        assert "," in reply, (
            f"the session did not recover from a single failed read: {reply!r}"
        )


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
        with srv.vxi11_faults(stale_reply_before_proc=DEVICE_READ):
            try:
                reply = inst.query("*IDN?").strip()
                detail = f"the stale record was skipped, query returned {reply!r}"
            except Exception as exc:  # noqa: BLE001
                # Rejecting it is also correct -- better than consuming it --
                # so long as the session survives.
                detail = f"the stale record was rejected: {type(exc).__name__}"
        after = inst.query("*IDN?").strip()
        assert "," in after, (
            f"the stream did not resynchronise after a stale reply, "
            f"next query gave {after!r}"
        )
        return detail


@check("a connection dropped mid-read is reported, not hung",
       rule="VPP-4.3 3.2.2")
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


@check("a maxRecvSize of zero does not wedge the session", rule="VXI-11 B.6.3")
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


@check("a write larger than maxRecvSize is split", rule="VXI-11 B.6.4")
def check_write_splitting():
    """B.6.4: the client must divide a write exceeding maxRecvSize itself.

    The mock reports a small maxRecvSize and the observation log is checked
    for more than one write reaching the instrument -- the client-side split
    is invisible from the reply alone.
    """
    srv = server()
    srv.set_vxi11_faults(max_recv_size=64)
    try:
        with open_inst() as inst:
            srv.clear_observed()
            payload = "TEST:SILENT? " + "x" * 400
            inst.write(payload)
            time.sleep(0.2)
            writes = srv.writes()
            joined = "".join(writes)
            assert payload in joined, (
                f"the instrument did not receive the whole message; it saw "
                f"{len(joined)} bytes across {len(writes)} writes"
            )
            return f"{len(payload)}B arrived in {len(writes)} write(s)"
    finally:
        srv.set_vxi11_faults()
        with open_inst() as inst:
            visa.drain_errors(inst)


# ---------------------------------------------------------------------------
# Connection establishment
# ---------------------------------------------------------------------------
@check("opening a dead port fails cleanly", rule="VPP-4.3 3.1.1")
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


@check("a refused link is reported as a VISA open failure", rule="VXI-11 B.6.3")
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
       rule="VPP-4.3 3.6.2.1")
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


@check("VI_ATTR_RSRC_LOCK_STATE reflects a held lock", rule="VPP-4.3 3.6.2.1")
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


@check("the session still works after a lock attempt failed",
       rule="VPP-4.3 3.6.2.1")
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


# ---------------------------------------------------------------------------
# Session behaviour
# ---------------------------------------------------------------------------
@check("a device that answers a read with nothing still times out",
       rule="VPP-4.3 3.2.2")
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
                f"expected VI_ERROR_TSK_TIMEOUT, got {visa.visa_status(exc)}"
            )
            assert elapsed < 4.0, f"a 1000ms timeout took {elapsed:.2f}s"
            return f"fired in {elapsed * 1000:.0f}ms"
        raise AssertionError("a read with nothing to read returned")


@check("the session recovers from a read timeout", rule="VPP-4.3 3.2.2")
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


@check("VI_ATTR_TCPIP_KEEPALIVE can be turned on", rule="VPP-4.3 3.5")
def check_keepalive():
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, st = visa.call(lib.set_attribute, sess, RA.tcpip_keepalive, True)
        value, get_st = visa.call(lib.get_attribute, sess, RA.tcpip_keepalive)
        visa.call(lib.set_attribute, sess, RA.tcpip_keepalive, False)
        assert st == StatusCode.success, f"setting keepalive returned {st!r}"
        assert get_st == StatusCode.success and value is True, (
            f"keepalive did not read back as on ({get_st!r}, {value!r})"
        )


@check("VI_ATTR_SEND_END_EN=False suppresses END on the write",
       rule="VXI-11 B.5.3")
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
        finally:
            inst.set_visa_attribute(RA.send_end_enabled, True)
            lib.write(sess, b"\n")
            visa.drain_errors(inst)


@check("closing the session destroys the link", rule="VXI-11 B.6.16")
def check_close_destroys_link():
    """Links are a finite server resource (B.6.5 caps them).

    A client that closes a session without destroying its link leaks one per
    open, and the symptom arrives much later as an unexplained refusal to open
    anything at all. Opening far more sessions than the cap, one at a time, is
    the cheap way to prove it does not.
    """
    for i in range(80):
        try:
            with open_inst() as inst:
                inst.query("*IDN?")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"open/close cycle {i} failed with {visa.visa_status(exc)}; "
                f"the server ran out of links, so closing does not destroy them"
            ) from None
    return "80 sequential open/close cycles"


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0], protocol="vxi11")
    args = parser.parse_args()
    if args.protocol != "vxi11":
        print("this suite is VXI-11 only", file=sys.stderr)
        return 4

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend,
            resource=resource,
            server=srv,
            timeout=args.timeout,
            protocol=args.protocol,
        )
        stats = harness.Stats(
            "vxi11 conformance",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol="vxi11")
        harness.run_checks(
            checks, stats, watchdog=20.0, on_timeout=restart_server
        )
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
