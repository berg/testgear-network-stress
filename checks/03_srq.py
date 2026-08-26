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
                inst.enable_event(visa.SRQ, visa.QUEUE)
                received = 0
                missed = 0
                for _ in range(args.iterations):
                    srq_trigger(inst)
                    try:
                        inst.wait_on_event(visa.SRQ, int(args.settle * 1000 + 2000))
                        received += 1
                    except Exception:  # noqa: BLE001
                        missed += 1
                stats.check(
                    missed == 0,
                    f"{received}/{args.iterations} queued SRQs arrived",
                    rule="VPP-4.3 3.4.1",
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
                inst.enable_event(visa.SRQ, visa.HANDLER)
                try:
                    for i in range(min(args.iterations, 50)):
                        done.clear()
                        srq_trigger(inst)
                        if not done.wait(5.0):
                            stats.error(f"handler {i} never ran (deadlock?)")
                            break
                    else:
                        stats.check(True, f"{len(fired)} handler callbacks ran")
                    stats.check(
                        not handler_errors,
                        f"read_stb from inside the handler works "
                        f"({handler_errors[:3]})",
                    )
                    stats.check(
                        all(isinstance(s, int) for s in fired),
                        "every handler saw a real status byte",
                    )
                    if fired:
                        stats.note(
                            f"status bytes seen: "
                            f"{sorted({hex(s) for s in fired})}"
                        )
                finally:
                    inst.disable_event(visa.SRQ, visa.HANDLER)
                    inst.uninstall_handler(visa.SRQ, handler, wrapped)

                # -- 3. SRQs racing against concurrent status queries --------
                # This is the interleaving that used to corrupt a response: a
                # service request landing between a status query and its
                # answer.
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
                    f"status queries stayed intact while SRQs fired "
                    f"({bad_stb[:3]})",
                    rule="VPP-4.3 3.3.1",
                )
                drained = 0
                while drained < 10000:
                    response = inst.wait_on_event(visa.SRQ, 0, capture_timeout=True)
                    if response.timed_out:
                        break
                    drained += 1
                stats.note(f"{drained} service requests queued during the race")
                stats.check(
                    drained > 0, "the race actually produced service requests"
                )
                inst.disable_event(visa.SRQ, visa.QUEUE)

                # -- 4. after all that, the session is still sane ------------
                stats.check(
                    inst.query("*IDN?").strip() == idn,
                    "session healthy after SRQ load",
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

        if args.report:
            stats.write_report(args.report)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
