#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hammer the service request path.

The interesting case is a service request arriving while an unrelated
asynchronous exchange is in flight. Before the channel was demultiplexed that
would be mistaken for the exchange's response, so every status query here is
checked for a plausible value rather than just for not raising.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}

#: A service request over VXI-11 takes about a second to arrive, because
#: pyvisa-py does not acknowledge device_intr_srq. The loops here are sized in
#: iterations, so the wall clock they need depends entirely on the transport,
#: and the default watchdog is nowhere near it.
DELIVERY_WATCHDOG = 900.0
HANDLER_WATCHDOG = 400.0
RACE_WATCHDOG = 300.0


def add_arguments(parser) -> None:
    parser.add_argument(
        "--settle", type=float, default=0.05, help="seconds to wait per SRQ"
    )


def srq_trigger(inst) -> None:
    """Provoke one service request via the operation-complete bit.

    `*OPC` sets ESR bit 0; `*ESE 1` summarises that into ESB; `*SRE 32`
    enables ESB in the service-request mask. Going through the instrument's
    own status model rather than forcing a status byte is deliberate: it is
    the instrument that decides to pull the line, and a service request the
    harness fabricated would not exercise that decision.
    """
    inst.write("*CLS")
    inst.write("*ESE 1")   # OPC -> ESB
    inst.write("*SRE 32")  # ESB -> RQS
    inst.write("*OPC")


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        STATE["idn"] = session.query("*IDN?").strip()
        visa.drain_errors(session)
        try:
            yield
        finally:
            # Leave the status model as it was found: these are sticky, and a
            # later script inheriting a service-request mask sees SRQs it
            # never asked for.
            for command in ("*CLS", "*SRE 0", "*ESE 0"):
                try:
                    session.write(command)
                except Exception:  # noqa: BLE001
                    break
            visa.check_errors(session, ctx["stats"], "at end of run")


# -- 1. queued delivery, repeatedly ------------------------------------------
@check("SRQ events can be enabled for queued delivery", rule="VPP-4.3 3.7.6")
def check_enable_queued():
    """Enabling the event is itself a check.

    Upstream pyvisa-py answers VI_ERROR_INV_EVENT for SRQ over HiSLIP, and
    pyvisa turns that into a raise -- which used to take the whole script with
    it and leave thirty checks missing from the column rather than one of them
    failing. As a check of its own it costs one row, and the checks that
    depend on it skip under their own names.
    """
    CTX["session"].enable_event(visa.SRQ, visa.QUEUE)
    STATE["queued_ok"] = True
    return (
        "viEnableEvent(VI_EVENT_SERVICE_REQ, VI_QUEUE) returned without raising"
    )


@check("every queued service request is delivered", rule="VPP-4.3 3.4.1",
       watchdog=DELIVERY_WATCHDOG)
def check_queued_delivery():
    if not STATE.get("queued_ok"):
        raise Skip(
            "queued delivery could not be enabled, so no service request "
            "could be waited for"
        )
    args, inst, stats = CTX["args"], CTX["session"], CTX["stats"]
    received = missed = 0
    try:
        for _ in range(args.iterations):
            srq_trigger(inst)
            try:
                inst.wait_on_event(visa.SRQ, int(args.settle * 1000 + 2000))
                received += 1
            except Exception:  # noqa: BLE001
                missed += 1
    finally:
        if args.protocol == "vxi11":
            # Not a failure, but the reason this section is slow, and worth
            # saying out loud so nobody spends an afternoon on it twice:
            # pyvisa-py does not acknowledge device_intr_srq (B.6.30, a void
            # reply is still a reply), so a server that waits for it pays its
            # timeout on every service request after the first. See
            # docs/findings.md.
            stats.note(
                "VXI-11 service requests are delivered about one per second "
                "here because the interrupt RPC goes unacknowledged; HiSLIP "
                "delivers them immediately"
            )
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.QUEUE)
                inst.discard_events(visa.SRQ, visa.QUEUE)
    detail = f"{received}/{args.iterations} arrived"
    assert missed == 0, detail
    return detail


# -- 2. handler delivery, including I/O from the handler ---------------------
def handler_run() -> dict:
    """Install a handler, drive it, take it back down again -- once.

    Four checks read this one run, so it happens on the first of them and the
    rest read what it recorded. Keeping install, enable, load and teardown in
    a single function is what makes that safe: a handler left installed while
    the race section re-enables the queue is a different test than the one
    written here.
    """
    if "handler" in STATE:
        return STATE["handler"]

    args, inst = CTX["args"], CTX["session"]
    result: dict = {
        "enabled": False,
        "enable_error": None,
        "fired": [],
        "handler_errors": [],
        "never_ran": None,
    }
    STATE["handler"] = result
    fired: list[int] = result["fired"]
    handler_errors: list[str] = result["handler_errors"]
    done = threading.Event()

    def handler(session, event_type, context, user_handle):
        # Reading the status byte from inside the handler is the whole point
        # of an SRQ; it must not deadlock against the channel reader.
        try:
            fired.append(inst.read_stb())
        except Exception as exc:  # noqa: BLE001
            handler_errors.append(repr(exc))
        finally:
            done.set()

    wrapped = inst.install_handler(visa.SRQ, handler)
    try:
        try:
            inst.enable_event(visa.SRQ, visa.HANDLER)
        except Exception as exc:  # noqa: BLE001
            result["enable_error"] = exc
            return result
        result["enabled"] = True

        for i in range(min(args.iterations, 50)):
            done.clear()
            srq_trigger(inst)
            if not done.wait(5.0):
                result["never_ran"] = i
                break
    finally:
        if result["enabled"]:
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.HANDLER)
        with contextlib.suppress(Exception):
            inst.uninstall_handler(visa.SRQ, handler, wrapped)
    return result


def handler_load() -> dict:
    """The handler run, or a skip saying why the checks on it cannot run."""
    result = handler_run()
    if not result["enabled"]:
        raise Skip("handler delivery could not be enabled")
    return result


@check("SRQ events can be enabled for handler delivery", rule="VPP-4.3 3.7.6",
       watchdog=HANDLER_WATCHDOG)
def check_enable_handler():
    result = handler_run()
    if result["enable_error"] is not None:
        raise AssertionError(
            f"{type(result['enable_error']).__name__}: {result['enable_error']}"
        )
    return (
        "viEnableEvent(VI_EVENT_SERVICE_REQ, VI_HNDLR) returned without raising"
    )


@check("every service request runs the installed handler",
       watchdog=HANDLER_WATCHDOG)
def check_handler_runs():
    result = handler_load()
    assert result["never_ran"] is None, (
        f"handler {result['never_ran']} never ran (deadlock?)"
    )
    return f"{len(result['fired'])} callbacks ran"


@check("read_stb from inside the handler works", watchdog=HANDLER_WATCHDOG)
def check_read_stb_in_handler():
    result = handler_load()
    errors, fired = result["handler_errors"], result["fired"]
    detail = f"{len(fired)} calls from inside the handler, " + (
        f"{len(errors)} raised: {errors[:3]}" if errors else "none raised"
    )
    assert not errors, detail
    return detail


@check("every handler saw a real status byte", rule="VPP-4.3 3.4.1",
       watchdog=HANDLER_WATCHDOG)
def check_handler_status_bytes():
    """A status byte is one byte: that is the claim.

    `all(isinstance(s, int) ...)` was the whole assertion here, which is true
    of any int and true of an empty list -- so the check passed on a run where
    no handler ran at all, and passed on R&S with a 0x00 among the bytes.

    RQS (bit 6) is reported, not asserted. It is what marks the byte as the
    one that requested service, but the poller threads in section 3 read the
    status byte too and reading it clears RQS, so a handler can legitimately
    arrive to find it already taken.
    """
    fired = handler_load()["fired"]
    with_rqs = sum(1 for s in fired if isinstance(s, int) and s & 0x40)
    detail = (
        f"{len(fired)} callbacks, {with_rqs} with RQS set, "
        f"first few {fired[:3]}"
    )
    if fired:
        CTX["stats"].note(
            f"status bytes seen: {sorted({hex(s) for s in fired})}"
        )
    assert fired and all(
        isinstance(s, int) and 0 <= s <= 0xFF for s in fired
    ), detail
    return detail


# -- 3. SRQs racing against concurrent status queries ------------------------
@check("SRQ events can be re-enabled after handler delivery",
       rule="VPP-4.3 3.7.6")
def check_reenable_queue():
    CTX["session"].enable_event(visa.SRQ, visa.QUEUE)
    STATE["race_ok"] = True
    return (
        "viEnableEvent(VI_EVENT_SERVICE_REQ, VI_QUEUE) returned without "
        "raising a second time"
    )


@check("status queries stayed intact while SRQs fired", rule="VPP-4.3 3.3.1",
       watchdog=RACE_WATCHDOG)
def check_status_queries_intact():
    """The interleaving that used to corrupt a response: a service request
    landing between a status query and its answer."""
    args, inst = CTX["args"], CTX["session"]
    bad_stb: list[str] = []
    stop = threading.Event()

    def poller():
        while not stop.is_set():
            try:
                stb = inst.read_stb()
            except Exception as exc:  # noqa: BLE001
                bad_stb.append(f"read_stb raised {type(exc).__name__}: {exc}")
                continue
            # A status byte is one byte; anything else means the response was
            # not the one we asked for.
            if not isinstance(stb, int) or not 0 <= stb <= 0xFF:
                bad_stb.append(f"implausible status byte {stb!r}")

    threads = [threading.Thread(target=poller, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(args.iterations):
            srq_trigger(inst)
            time.sleep(args.settle)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5.0)

    detail = f"3 pollers against {args.iterations} service requests, " + (
        f"{len(bad_stb)} bad reads: {bad_stb[:3]}" if bad_stb else "no bad reads"
    )
    assert not bad_stb, detail
    return detail


@check("the race actually produced service requests", watchdog=RACE_WATCHDOG)
def check_race_produced_srqs():
    """Bounded by wall clock as well as by count.

    The race can queue thousands of events, and draining them one at a time
    dominated the whole suite's runtime -- 233s for this script against the
    mock, almost all of it here. The exact number is not the point of the
    check; that any arrived is.
    """
    if not STATE.get("race_ok"):
        raise Skip(
            "the queue could not be re-enabled after handler delivery, so "
            "there was nothing to drain"
        )
    inst, stats = CTX["session"], CTX["stats"]
    drained = 0
    drain_until = time.time() + 5.0
    try:
        while drained < 10000 and time.time() < drain_until:
            response = inst.wait_on_event(visa.SRQ, 0, capture_timeout=True)
            if response.timed_out:
                break
            drained += 1
        capped = " (drain capped)" if time.time() >= drain_until else ""
        stats.note(f"{drained} service requests queued during the race{capped}")
    finally:
        with contextlib.suppress(Exception):
            inst.disable_event(visa.SRQ, visa.QUEUE)
    detail = f"{drained} queued during the race{capped}"
    assert drained > 0, detail
    return detail


# -- 4. after all that, the session is still sane ----------------------------
@check("the session is healthy after the SRQ load")
def check_session_healthy():
    final = CTX["session"].query("*IDN?").strip()
    assert final == STATE["idn"], f"got {final!r}"
    return f"got {final!r}"


if __name__ == "__main__":
    script.run()
