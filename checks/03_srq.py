#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hammer the service request path.

The interesting case is a service request arriving while an unrelated
asynchronous exchange is in flight. Before the channel was demultiplexed that
would be mistaken for the exchange's response, so every status query here is
checked for a plausible value rather than just for not raising.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import cli, harness, visa  # noqa: E402


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


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    parser.add_argument(
        "--settle", type=float, default=0.05, help="seconds to wait per SRQ"
    )
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"srq ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            idn = inst.query("*IDN?").strip()
            visa.drain_errors(inst)

            try:
                # -- 1. queued delivery, repeatedly -------------------------
                # Enabling the event is itself a check. Upstream pyvisa-py
                # answers VI_ERROR_INV_EVENT for SRQ over HiSLIP, and pyvisa
                # turns that into a raise -- which used to take the whole
                # script with it and leave thirty checks missing from the
                # column rather than one of them failing.
                with stats.attempt(
                    "SRQ events can be enabled for queued delivery",
                    rule="VPP-4.3 3.7.6",
                    detail="viEnableEvent(VI_EVENT_SERVICE_REQ, VI_QUEUE) "
                    "returned without raising",
                ) as queued_ok:
                    inst.enable_event(visa.SRQ, visa.QUEUE)

                if queued_ok:
                    received = 0
                    missed = 0
                    for _ in range(args.iterations):
                        srq_trigger(inst)
                        try:
                            inst.wait_on_event(
                                visa.SRQ, int(args.settle * 1000 + 2000)
                            )
                            received += 1
                        except Exception:  # noqa: BLE001
                            missed += 1
                    stats.check(
                        missed == 0,
                        "every queued service request is delivered",
                        rule="VPP-4.3 3.4.1",
                        detail=f"{received}/{args.iterations} arrived",
                    )
                else:
                    # The delivery check cannot run, and saying so is not
                    # optional: leaving it unrecorded published a blank cell
                    # for the column that most needed one, and a blank cell
                    # reads as "not applicable" rather than "could not run".
                    stats.skip(
                        "every queued service request is delivered",
                        "queued delivery could not be enabled, so no service "
                        "request could be waited for",
                    )
                if args.protocol == "vxi11":
                    # Not a failure, but the reason this section is slow, and
                    # worth saying out loud so nobody spends an afternoon on
                    # it twice: pyvisa-py does not acknowledge device_intr_srq
                    # (B.6.30, a void reply is still a reply), so a server that
                    # waits for it pays its timeout on every service request
                    # after the first. See docs/findings.md.
                    stats.note(
                        "VXI-11 service requests are delivered about one per "
                        "second here because the interrupt RPC goes "
                        "unacknowledged; HiSLIP delivers them immediately"
                    )
                    inst.disable_event(visa.SRQ, visa.QUEUE)
                    inst.discard_events(visa.SRQ, visa.QUEUE)

                # -- 2. handler delivery, including I/O from the handler -----
                # Reading the status byte from inside the handler is the whole
                # point of an SRQ; it must not deadlock against the channel
                # reader.
                fired: list[int] = []
                handler_errors: list[str] = []
                done = threading.Event()

                def handler(session, event_type, context, user_handle):
                    try:
                        fired.append(inst.read_stb())
                    except Exception as exc:  # noqa: BLE001
                        handler_errors.append(repr(exc))
                    finally:
                        done.set()

                wrapped = inst.install_handler(visa.SRQ, handler)
                with stats.attempt(
                    "SRQ events can be enabled for handler delivery",
                    rule="VPP-4.3 3.7.6",
                    detail="viEnableEvent(VI_EVENT_SERVICE_REQ, VI_HNDLR) "
                    "returned without raising",
                ) as handler_ok:
                    inst.enable_event(visa.SRQ, visa.HANDLER)
                try:
                    if not handler_ok:
                        raise harness.Skip(
                            "handler delivery could not be enabled"
                        )
                    for i in range(min(args.iterations, 50)):
                        done.clear()
                        srq_trigger(inst)
                        if not done.wait(5.0):
                            stats.error(
                                "every service request runs the installed handler",
                                detail=f"handler {i} never ran (deadlock?)",
                            )
                            break
                    else:
                        stats.check(
                            True,
                            "every service request runs the installed handler",
                            detail=f"{len(fired)} callbacks ran",
                        )
                    stats.check(
                        not handler_errors,
                        "read_stb from inside the handler works",
                        detail=f"{len(fired)} calls from inside the handler, "
                        + (
                            f"{len(handler_errors)} raised: {handler_errors[:3]}"
                            if handler_errors
                            else "none raised"
                        ),
                    )
                    # `all(isinstance(s, int) ...)` was the whole assertion
                    # here, which is true of any int and true of an empty
                    # list -- so the check passed on a run where no handler
                    # ran at all, and passed on R&S with a 0x00 among the
                    # bytes. A status byte is one byte: that is the claim.
                    #
                    # RQS (bit 6) is reported, not asserted. It is what marks
                    # the byte as the one that requested service, but the
                    # poller threads in section 3 read the status byte too and
                    # reading it clears RQS, so a handler can legitimately
                    # arrive to find it already taken.
                    with_rqs = sum(1 for s in fired if isinstance(s, int) and s & 0x40)
                    stats.check(
                        bool(fired)
                        and all(isinstance(s, int) and 0 <= s <= 0xFF for s in fired),
                        "every handler saw a real status byte",
                        rule="VPP-4.3 3.4.1",
                        detail=f"{len(fired)} callbacks, {with_rqs} with RQS "
                        f"set, first few {fired[:3]}",
                    )
                    if fired:
                        stats.note(
                            f"status bytes seen: "
                            f"{sorted({hex(s) for s in fired})}"
                        )
                except harness.Skip as exc:
                    # The enable already recorded its own FAIL. Say once that
                    # the checks depending on it are absent rather than
                    # passing, and carry on to the next section.
                    stats.skip("the handler-delivery checks", str(exc))
                finally:
                    if handler_ok:
                        inst.disable_event(visa.SRQ, visa.HANDLER)
                    inst.uninstall_handler(visa.SRQ, handler, wrapped)

                # -- 3. SRQs racing against concurrent status queries --------
                # This is the interleaving that used to corrupt a response: a
                # service request landing between a status query and its
                # answer.
                with stats.attempt(
                    "SRQ events can be re-enabled after handler delivery",
                    rule="VPP-4.3 3.7.6",
                    detail="viEnableEvent(VI_EVENT_SERVICE_REQ, VI_QUEUE) "
                    "returned without raising a second time",
                ) as race_ok:
                    inst.enable_event(visa.SRQ, visa.QUEUE)
                bad_stb: list[str] = []
                stop = threading.Event()

                def poller():
                    while not stop.is_set():
                        try:
                            stb = inst.read_stb()
                        except Exception as exc:  # noqa: BLE001
                            bad_stb.append(
                                f"read_stb raised {type(exc).__name__}: {exc}"
                            )
                            continue
                        # A status byte is one byte; anything else means the
                        # response was not the one we asked for.
                        if not isinstance(stb, int) or not 0 <= stb <= 0xFF:
                            bad_stb.append(f"implausible status byte {stb!r}")

                threads = [
                    threading.Thread(target=poller, daemon=True) for _ in range(3)
                ]
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

                stats.check(
                    not bad_stb,
                    "status queries stayed intact while SRQs fired",
                    rule="VPP-4.3 3.3.1",
                    detail=f"3 pollers against {args.iterations} service "
                    f"requests, "
                    + (
                        f"{len(bad_stb)} bad reads: {bad_stb[:3]}"
                        if bad_stb
                        else "no bad reads"
                    ),
                )
                # Bounded by wall clock as well as by count. The race can
                # queue thousands of events, and draining them one at a time
                # dominated the whole suite's runtime -- 233s for this script
                # against the mock, almost all of it here. The exact number is
                # not the point of the check; that any arrived is.
                if race_ok:
                    drained = 0
                    drain_until = time.time() + 5.0
                    while drained < 10000 and time.time() < drain_until:
                        response = inst.wait_on_event(
                            visa.SRQ, 0, capture_timeout=True
                        )
                        if response.timed_out:
                            break
                        drained += 1
                    capped = (
                        " (drain capped)" if time.time() >= drain_until else ""
                    )
                    stats.note(
                        f"{drained} service requests queued during the "
                        f"race{capped}"
                    )
                    stats.check(
                        drained > 0,
                        "the race actually produced service requests",
                        detail=f"{drained} queued during the race{capped}",
                    )
                    inst.disable_event(visa.SRQ, visa.QUEUE)
                else:
                    stats.skip(
                        "the race actually produced service requests",
                        "the queue could not be re-enabled after handler "
                        "delivery, so there was nothing to drain",
                    )

                # -- 4. after all that, the session is still sane ------------
                final = inst.query("*IDN?").strip()
                stats.check(
                    final == idn,
                    "the session is healthy after the SRQ load",
                    detail=f"got {final!r}",
                )
                visa.check_errors(inst, stats, "at end of run")
            finally:
                # Leave the status model as it was found: these are sticky,
                # and a later script inheriting a service-request mask sees
                # SRQs it never asked for.
                for command in ("*CLS", "*SRE 0", "*ESE 0"):
                    try:
                        inst.write(command)
                    except Exception:  # noqa: BLE001
                        break

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
