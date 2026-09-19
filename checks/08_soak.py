#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Randomised mixed workload for a fixed duration.

Picks operations at random so orderings the scripted checks never produce get
exercised. Every query result is verified, so a desynchronised stream is caught
rather than silently tolerated.

    ./08_soak.py --duration 300        # five minutes
"""

from __future__ import annotations

import contextlib
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import check  # noqa: E402

CTX: dict = {}
STATE: dict = {}


def add_arguments(parser) -> None:
    parser.add_argument(
        "--duration", type=float, default=60.0, help="seconds to run for"
    )
    parser.add_argument("--seed", type=int, default=0, help="RNG seed")
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="include viAssertTrigger in the mix. Off by default: on an M8132A "
        "a device clear landing while the instrument is still working through "
        "back-to-back triggers makes it reset the connection, which is a "
        "pre-existing instrument-side fault and would end the soak early",
    )
    parser.add_argument(
        "--srq-thread",
        action="store_true",
        help="also run a background SRQ handler doing status queries",
    )


def ren_modes(protocol: str) -> list:
    """Remote/local codes the transport actually carries.

    Picking a refused code at random would report a failure on roughly half
    the REN operations over VXI-11, for a reason the soak is not about.
    """
    if protocol == "vxi11":
        return [
            constants.RENLineOperation.asrt_address,
            constants.RENLineOperation.asrt_address_llo,
            constants.RENLineOperation.address_gtl,
            constants.RENLineOperation.deassert_gtl,
        ]
    return list(constants.RENLineOperation)


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        STATE["idn"] = session.query("*IDN?").strip()
        STATE["big_query"] = visa.resolve_big_query(
            ctx["args"], ctx["server"], session, ctx["stats"]
        )
        # Whether the reference is worth comparing against byte for byte.
        # Against the mock TEST:BIG? is fixed; against an instrument the
        # default is *LRN?, whose answer this very script then spends a
        # minute changing.
        STATE["stable"] = bool(STATE["big_query"]) and visa.big_query_is_stable(
            session, STATE["big_query"], ctx["stats"]
        )
        STATE["big"] = (
            session.query(STATE["big_query"]) if STATE["big_query"] else None
        )
        visa.drain_errors(session)
        # The mix contains viClear and a partial read that stops early by
        # design, either of which leaves a query unfinished. Over thousands of
        # operations at least one lands next to a write.
        ctx["stats"].expect_desync(
            (-410, -420),
            "the operation mix includes device clear and a deliberately "
            "partial read, both of which abandon a query in flight",
        )
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")
            for command in ("*CLS", "*SRE 0"):
                try:
                    session.write(command)
                except Exception:  # noqa: BLE001
                    break


@check("the mixed-operation soak ran to completion", watchdog=0)
def check_soak():
    """Run the random mix until `--duration` is up.

    No watchdog: the caller chooses how long this runs, so no constant could
    be right for both a one-minute smoke soak and an overnight one. The loop's
    own deadline is what bounds it, and the operations inside it each catch
    their own exceptions.

    Failures are recorded under a name naming the operation that produced
    them, as they happen. This check is the verdict on the run as a whole:
    that it got to the end.
    """
    args, stats, inst = CTX["args"], CTX["stats"], CTX["session"]
    lib, sess = inst.visalib, inst.session
    rng = random.Random(args.seed)
    idn, big, big_query = STATE["idn"], STATE["big"], STATE["big_query"]
    stable = STATE.get("stable", True)

    def big_reply_is_wrong(got: str) -> bool:
        """Whether a large reply came back damaged, as far as we can tell.

        An unstable query only licenses the length compare; see
        `visa.big_query_is_stable`.
        """
        return got != big if stable else len(got) != len(big)

    handler = wrapped = None
    srq_count = [0]
    if args.srq_thread:

        def handler(session, event_type, context, user_handle):  # noqa: F811
            try:
                inst.read_stb()
                srq_count[0] += 1
            except Exception as exc:  # noqa: BLE001
                stats.error("SRQ handler failed", exc)

        wrapped = inst.install_handler(visa.SRQ, handler)
        inst.enable_event(visa.SRQ, visa.HANDLER)

    modes = ren_modes(args.protocol)
    # The attribute to poke at random has to exist on this transport, or every
    # "attr" operation reports a failure about the check rather than about the
    # session.
    attribute = (
        constants.ResourceAttribute.tcpip_hislip_max_message_kb
        if args.protocol == "hislip"
        else constants.ResourceAttribute.tcpip_address
    )

    tally: dict[str, int] = {}
    deadline = time.time() + args.duration
    operations = [
        "query", "query", "query",
        "read_stb", "ren", "lock", "clear", "flush", "attr",
    ]
    if big:
        operations += ["big_query", "partial_read"]
    if args.srq_thread:
        operations.append("srq")
    if args.trigger:
        operations.append("trigger")

    last_report = time.time()
    try:
        while time.time() < deadline:
            op = rng.choice(operations)
            tally[op] = tally.get(op, 0) + 1
            try:
                if op == "query":
                    got = inst.query("*IDN?").strip()
                    if got != idn:
                        stats.error(
                            "*IDN? answers correctly under the soak mix",
                            detail=f"returned {got!r}",
                        )
                elif op == "big_query":
                    if big_reply_is_wrong(inst.query(big_query)):
                        stats.error(
                            "large query returned the wrong bytes"
                            if stable
                            else "large query returned the wrong number of bytes"
                        )
                elif op == "partial_read":
                    lib.write(sess, big_query.encode() + b"\n")
                    collected = bytearray()
                    # Read until the instrument says END, not until the
                    # reference length is reached. Those are the same number
                    # only while the reply is the size it was at startup, and
                    # a reply that grew leaves its tail in the stream --
                    # which the next operation then reads as its own answer.
                    # One such read desynchronises everything after it, which
                    # is what 72 "wrong bytes" failures in a single 34465A run
                    # actually were: one unread tail, echoing.
                    ended = False
                    # Bounded, because this check runs under `watchdog=0` --
                    # the soak's duration is the caller's to choose, so no
                    # constant could be right for it -- and a loop with no
                    # watchdog over it is the one shape that turns a
                    # misbehaving instrument into a suite that never returns.
                    # A single byte at a time is the worst case, so allow the
                    # reference length in reads plus slack, then give up.
                    for _ in range(len(big) + 64):
                        data, st = visa.call(
                            lib.read, sess, rng.choice((1, 13, 512, 8192))
                        )
                        if not data and st != StatusCode.success:
                            break
                        collected.extend(data)
                        if st == StatusCode.success:
                            ended = True
                            break
                    if not ended:
                        # The reply never ended: either it stopped early or
                        # it outran the buffer. Resynchronise before the next
                        # operation inherits the remains.
                        visa.status(lib.clear, sess)
                    if big_reply_is_wrong(collected.decode("latin-1")):
                        stats.error("a chunked read reassembled incorrectly")
                elif op == "read_stb":
                    stb = inst.read_stb()
                    if not 0 <= stb <= 0xFF:
                        stats.error(
                            "read_stb returns a plausible status byte under "
                            "the soak mix",
                            detail=f"got {stb!r}",
                        )
                elif op == "trigger":
                    visa.status(
                        lib.assert_trigger, sess, constants.TriggerProtocol.default
                    )
                elif op == "ren":
                    mode = rng.choice(modes)
                    st = visa.status(lib.gpib_control_ren, sess, mode)
                    if st != StatusCode.success:
                        stats.error(
                            f"REN {mode.name} is accepted under the soak mix",
                            detail=f"got {st!r}",
                        )
                elif op == "lock":
                    kinds = [constants.Lock.exclusive]
                    if args.protocol == "hislip":
                        kinds.append(constants.Lock.shared)
                    _, st = visa.call(lib.lock, sess, rng.choice(kinds), 1000, None)
                    if st == StatusCode.success:
                        visa.status(lib.unlock, sess)
                    elif st != StatusCode.error_timeout:
                        stats.error(
                            "viLock succeeds or times out under the soak mix",
                            detail=f"got {st!r}",
                        )
                elif op == "clear":
                    if visa.status(lib.clear, sess) != StatusCode.success:
                        stats.error("viClear failed")
                elif op == "flush":
                    # Not asserted on: viFlush is unimplemented on some
                    # transports, and that gap is reported once by the smoke
                    # suite rather than thousands of times here.
                    visa.status(
                        lib.flush,
                        sess,
                        constants.BufferOperation.discard_read_buffer,
                    )
                elif op == "attr":
                    visa.call(lib.get_attribute, sess, attribute)
                elif op == "srq":
                    for command in ("*CLS", "*ESE 1", "*SRE 32", "*OPC"):
                        inst.write(command)
            except Exception as exc:  # noqa: BLE001
                stats.error(
                    f"the {op} operation never raises under the soak mix", exc
                )
                # Once the connection is gone every further operation fails
                # instantly, which buries the first failure under thousands of
                # identical ones. Stop instead.
                if visa.is_connection_lost(exc) or "onnection" in str(exc):
                    stats.note(
                        f"connection lost after {sum(tally.values())} "
                        f"operations, stopping"
                    )
                    break
                # Otherwise try to get back to a sane state and keep going.
                try:
                    lib.clear(sess)
                except Exception:  # noqa: BLE001
                    break

            if time.time() - last_report >= 15:
                done = sum(tally.values())
                stats.note(
                    f"{done} operations, {len(stats.failures)} failures, "
                    f"{deadline - time.time():.0f}s left"
                )
                last_report = time.time()
    finally:
        stats.note(
            "operation mix: "
            + ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        )
        if args.srq_thread:
            stats.note(f"{srq_count[0]} service requests handled")
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.HANDLER)
            with contextlib.suppress(Exception):
                inst.uninstall_handler(visa.SRQ, handler, wrapped)
    return f"{sum(tally.values())} operations"


@check("the session is healthy at the end of the soak")
def check_healthy_after_soak():
    """Did the session survive, and if not, does a clear bring it back?

    This used to be a bare `query`, so a session the soak had broken ended
    the run with a thirty-line traceback filed as "unexpected exception".
    That is the least useful rendering of the most interesting result here:
    the answer wanted is which VISA status came back, and whether the session
    was wedged or merely desynchronised -- a distinction viClear exists to
    draw, and the one a caller needs to know whether the instrument needs
    power cycling before the next run.
    """
    inst = CTX["session"]
    try:
        final = inst.query("*IDN?").strip()
    except Exception as exc:  # noqa: BLE001
        status = visa.visa_status(exc)
        try:
            inst.clear()
            recovered = inst.query("*IDN?").strip()
        except Exception as after:  # noqa: BLE001
            raise AssertionError(
                f"the session did not answer after the soak ({status}) and a "
                f"device clear did not revive it ({visa.visa_status(after)}); "
                f"it is wedged, not desynchronised"
            ) from None
        raise AssertionError(
            f"the session did not answer after the soak ({status}), though a "
            f"device clear revived it and it then returned {recovered!r}; the "
            f"soak left the message flow desynchronised rather than dead"
        ) from None
    assert final == STATE["idn"], f"got {final!r}"
    return f"got {final!r}"


if __name__ == "__main__":
    script.run()
