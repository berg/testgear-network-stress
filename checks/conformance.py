#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Client conformance: what a VISA implementation must do on the wire.

Each check states the rule it rests on, so a failure is a bug report rather
than an opinion. The rules come from VPP-4.3 (*The VISA Library*), VXI-11 Rev
1.0 (*TCP/IP Instrument Protocol Specification*) and IVI-6.1 (*HiSLIP*); the
documents are not vendored here -- they are IVI Foundation and VXIbus
Consortium copyright -- so a citation names the clause and you read it there.

The interesting checks are the ones the fault injector makes possible. A bench
produces a connection that dies mid-read about once a year and never when
somebody is watching; here it is a knob. Those checks are marked "injected".
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants, errors  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

#: Filled in by main() so the checks can reach the target without threading
#: it through every signature. Module state is normally a smell; here it keeps
#: the check bodies readable, which is what anybody reading a failure needs.
CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def server():
    """The mock server, or skip: this check injects a fault."""
    if CTX.get("server") is None:
        raise Skip("needs the mock server's fault injection; not available against real hardware")
    return CTX["server"]


# ---------------------------------------------------------------------------
# I/O and status
# ---------------------------------------------------------------------------
@check("a query returns the instrument's reply", rule="VPP-4.3 3.2.1")
def check_query():
    with open_inst() as inst:
        reply = inst.query("*IDN?").strip()
        assert "," in reply, f"expected a comma-separated *IDN?, got {reply!r}"
        return reply


@check("a second query on the same session still works", rule="VPP-4.3 3.2.1")
def check_two_queries():
    with open_inst() as inst:
        first = inst.query("*IDN?").strip()
        second = inst.query("*IDN?").strip()
        assert first == second, (
            f"the same query gave different answers: {first!r} then {second!r}. "
            "That is a desynchronised message stream, not a slow instrument"
        )


@check("a read whose message ends exactly on the chunk size terminates",
       rule="VPP-4.3 RULE 6.1.1")
def check_exact_chunk_boundary():
    """The end-of-message-lost-to-the-byte-count bug, generalised.

    pyvisa's `read_raw` loops *while* the status is max-count, so a response
    whose length is an exact multiple of the chunk size triggers one more read
    that can only time out. Ask for a reply sized to land exactly on a
    boundary and require the read to finish inside the timeout.
    """
    srv = server()
    with open_inst() as inst:
        chunk = inst.chunk_size
        # The reply gets a trailing newline, so ask for one byte less than the
        # boundary to land the terminator exactly on it.
        srv.big_reply(chunk - 1)
        started = time.time()
        reply = inst.query("TEST:BIG?")
        elapsed = time.time() - started
        assert len(reply.strip()) == chunk - 1, (
            f"expected {chunk - 1} bytes back, got {len(reply.strip())}"
        )
        assert elapsed < CTX["timeout"] / 1000.0, (
            f"a reply landing exactly on the {chunk}-byte chunk boundary took "
            f"{elapsed:.2f}s, which means an extra read was issued and timed out"
        )
        return f"{chunk}-byte boundary, {elapsed * 1000:.0f}ms"


@check("a large reply is reassembled across chunks", rule="VPP-4.3 RULE 6.1.2")
def check_large_reply():
    srv = server()
    size = 64 * 1024
    with open_inst() as inst:
        srv.big_reply(size)
        reply = inst.query("TEST:BIG?").strip()
        assert len(reply) == size, f"expected {size} bytes, got {len(reply)}"
        # Byte-for-byte, not just the length: a desynchronised stream can
        # deliver the right *number* of bytes from the wrong message.
        expected = "".join(str(i % 10) for i in range(size))
        assert reply == expected, "the reassembled reply does not match what was sent"
        return f"{size} bytes"


@check("a reply split across many TCP segments is reassembled", rule="VPP-4.3 RULE 6.1.2")
def check_dribbled_reply():
    """injected: one byte per segment.

    Segment boundaries are not message boundaries. A client that assumes one
    recv() yields one reply works on a quiet bench and fails on a loaded
    network, which is the least reproducible bug in this whole area.
    """
    srv = server()
    with open_inst() as inst:
        srv.big_reply(200)
        with srv.faults(dribble=True):
            reply = inst.query("TEST:BIG?").strip()
        assert len(reply) == 200, (
            f"a 200-byte reply arriving one byte per segment came back as "
            f"{len(reply)} bytes"
        )
        return "200 segments, reassembled"


@check("read_stb reports the status byte", rule="VPP-4.3 3.3.1",
       protocols=("vxi11",))
def check_read_stb():
    """VXI-11 only, for the same reason as the silent-read check.

    VXI-11 `device_readstb` maps onto a real serial poll, so forcing the
    simulated device's status byte is visible to the client. HiSLIP does not
    poll on every status query: ugpibd's server synthesises the byte from its
    own view of pending output and leaves the rest to the SRQ forwarder, so a
    status forced at the device never reaches a HiSLIP client at all.

    That difference is the *server's*, and this suite is about clients, so
    asserting it here would be measuring the wrong thing. It is worth writing
    down as a server-side disparity in its own right -- see docs/findings.md
    -- but not worth failing a client for.
    """
    srv = server()
    with open_inst() as inst:
        inst.write("*CLS")
        srv.set_stb(0x01)
        inst.write("*SRE 1")
        stb = inst.read_stb()
        assert stb & 0x40, f"expected MSS set in the status byte, got {stb:#04x}"
        srv.set_stb(0x00)
        return f"stb={stb:#04x}"


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------
@check("a read with nothing to read reports a timeout", rule="VPP-4.3 3.2.2",
       protocols=("vxi11",))
def check_read_timeout():
    """VXI-11 only, and the asymmetry is the server's, not the client's.

    On VXI-11 a bus read that finds nothing maps onto the protocol's own
    timeout, which is what reaches the client. ugpibd's HiSLIP server makes
    the opposite call deliberately: an empty read is answered with an error
    rather than an empty message, on the grounds that a caller cannot tell a
    plausible empty string from a real one. So the same instrument condition
    is a timeout over one transport and an I/O error over the other, and
    asserting a timeout on HiSLIP would be asserting on that choice rather
    than on the client. The client-side deadline behaviour that check was
    reaching for is covered properly by the stalled-connection check, where
    the transport really does go quiet.
    """
    with open_inst() as inst:
        inst.timeout = 1000
        inst.write("TEST:SILENT?")
        started = time.time()
        try:
            inst.read()
        except errors.VisaIOError as exc:
            elapsed = time.time() - started
            assert exc.error_code == constants.StatusCode.error_timeout, (
                f"expected VI_ERROR_TSK_TIMEOUT, got {visa.visa_status(exc)}"
            )
            # A timeout that fires late is a deadline bug: the classic shape
            # is a per-recv() timeout applied repeatedly instead of one
            # deadline for the whole operation.
            assert elapsed < 3.0, (
                f"a 1000ms timeout took {elapsed:.2f}s to fire"
            )
            return f"fired in {elapsed * 1000:.0f}ms"
        raise AssertionError("a read with nothing to read returned instead of timing out")


@check("a connection lost mid-reply is reported, not hung", rule="VPP-4.3 3.2.2")
def check_connection_dropped():
    """injected: the server closes the socket partway through a reply."""
    srv = server()
    with open_inst() as inst:
        inst.timeout = 2000
        srv.big_reply(8192)
        started = time.time()
        with srv.faults(drop_after_bytes=64):
            try:
                inst.query("TEST:BIG?")
            except Exception as exc:
                elapsed = time.time() - started
                assert visa.is_connection_lost(exc) or (
                    isinstance(exc, errors.VisaIOError)
                    and exc.error_code == constants.StatusCode.error_timeout
                ), f"expected a connection-lost or timeout error, got {visa.visa_status(exc)}"
                assert elapsed < 5.0, f"took {elapsed:.2f}s to notice"
                lost = visa.is_connection_lost(exc)
                return (
                    f"reported as {visa.visa_status(exc)} in {elapsed * 1000:.0f}ms"
                    + ("" if lost else " (timeout, not connection-lost)")
                )
        raise AssertionError(
            "a connection dropped mid-reply did not raise; the client "
            "returned a truncated message as if it were complete"
        )


@check("a stalled connection times out rather than hanging", rule="VPP-4.3 3.2.2")
def check_stalled_connection():
    """injected: bytes stop arriving, socket stays open.

    This is the one that finds deadline bugs. The connection is alive, so
    there is no error to notice; only a correctly-computed deadline ends the
    read. A client that waits forever here hangs a whole acquisition.
    """
    srv = server()
    with open_inst() as inst:
        inst.timeout = 1500
        srv.big_reply(8192)
        started = time.time()
        with srv.faults(stall_after_bytes=64):
            try:
                inst.query("TEST:BIG?")
            except errors.VisaIOError as exc:
                elapsed = time.time() - started
                assert exc.error_code == constants.StatusCode.error_timeout, (
                    f"expected a timeout, got {visa.visa_status(exc)}"
                )
                assert elapsed < 6.0, (
                    f"a 1500ms timeout against a stalled connection took "
                    f"{elapsed:.2f}s, which means the deadline is being "
                    f"restarted per read rather than applied to the operation"
                )
                return f"timed out in {elapsed * 1000:.0f}ms"
        raise AssertionError("a stalled read returned instead of timing out")


@check("the session recovers after a timeout", rule="VPP-4.3 3.2.2")
def check_recovery_after_timeout():
    with open_inst() as inst:
        inst.timeout = 800
        inst.write("TEST:SILENT?")
        try:
            inst.read()
        except errors.VisaIOError:
            pass
        inst.clear()
        inst.timeout = CTX["timeout"]
        reply = inst.query("*IDN?").strip()
        assert "," in reply, (
            f"after a timeout and a device clear the session returned {reply!r}; "
            "the message stream did not resynchronise"
        )


# ---------------------------------------------------------------------------
# Session behaviour
# ---------------------------------------------------------------------------
@check("two sessions to the same instrument are independent", rule="VPP-4.3 3.1.3")
def check_parallel_sessions():
    with open_inst() as first, open_inst() as second:
        a = first.query("*IDN?").strip()
        b = second.query("*IDN?").strip()
        assert a == b, f"two sessions disagreed about *IDN?: {a!r} vs {b!r}"


@check("a closed session does not disturb the others", rule="VPP-4.3 3.1.3")
def check_close_isolation():
    with open_inst() as keep:
        with open_inst() as temporary:
            temporary.query("*IDN?")
        reply = keep.query("*IDN?").strip()
        assert "," in reply, (
            "closing one session broke another; the ResourceManager is shared "
            "per backend and closing it closes every session it owns"
        )


@check("concurrent queries on separate sessions stay in step", rule="VPP-4.3 3.1.3")
def check_concurrent_sessions():
    """A status query racing a write is how the _rmt data race presented.

    It showed up as the instrument answering the *next* command with
    -410 Query INTERRUPTED, about once per 20,000 concurrent status queries.
    """
    errors_seen: list[str] = []

    def worker(n: int):
        try:
            with open_inst() as inst:
                for _ in range(n):
                    reply = inst.query("*IDN?").strip()
                    if "," not in reply:
                        errors_seen.append(f"bad reply {reply!r}")
                    inst.read_stb()
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(20,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, f"{len(alive)} worker threads never finished"
    assert not errors_seen, f"concurrent sessions desynchronised: {errors_seen[:3]}"
    return "4 sessions x 20 query+poll cycles"


@check("the error queue is clean after a normal exchange", rule="SCPI-99 21.8")
def check_error_queue_clean():
    with open_inst() as inst:
        visa.drain_errors(inst)
        for _ in range(5):
            inst.query("*IDN?")
        left = visa.drain_errors(inst)
        desync = [e for e in left if _code(e) in visa.DESYNC_ERRORS]
        assert not desync, f"the exchange left I/O errors in the queue: {desync}"
        return "clean" if not left else f"non-I/O entries only: {left}"


def _code(entry: str) -> int:
    try:
        return int(entry.split(",")[0])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
@check("the client sent exactly the traffic the API implies", rule="VPP-4.3 3.2.1")
def check_observed_traffic():
    """What the client actually put on the bus, not just what came back.

    A transport bug can produce the right answer with the wrong traffic -- a
    duplicated write the instrument tolerates, a status query smuggled into
    the middle of a message. That is invisible from the client side, and it is
    exactly what this suite exists to catch.
    """
    srv = server()
    with open_inst() as inst:
        srv.clear_observed()
        inst.query("*IDN?")
        writes = [w.strip() for w in srv.writes()]
        assert writes == ["*IDN?"], (
            f"one query should put one write on the bus; the instrument saw {writes}"
        )
        reads = srv.count("read")
        assert reads == 1, f"one query should cause one read; the instrument saw {reads}"


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend, resource=resource, server=srv, timeout=args.timeout
        )
        stats = harness.Stats(
            f"conformance ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol=args.protocol)
        harness.run_checks(checks, stats, watchdog=30.0)
        if args.report:
            stats.write_report(args.report)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
