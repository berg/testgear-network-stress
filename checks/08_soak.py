#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Randomised mixed workload for a fixed duration.

Picks operations at random so orderings the scripted checks never produce get
exercised. Every query result is verified, so a desynchronised stream is caught
rather than silently tolerated.

    ./08_soak.py --duration 300        # five minutes
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402


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


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
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
    args = parser.parse_args()
    rng = random.Random(args.seed)

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"soak ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session
            idn = inst.query("*IDN?").strip()
            big_query = visa.resolve_big_query(args, srv, inst, stats)
            big = inst.query(big_query) if big_query else None
            visa.drain_errors(inst)

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
            # The attribute to poke at random has to exist on this transport,
            # or every "attr" operation reports a failure about the check
            # rather than about the session.
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
                        if inst.query(big_query) != big:
                            stats.error("large query returned the wrong bytes")
                    elif op == "partial_read":
                        lib.write(sess, big_query.encode() + b"\n")
                        collected = bytearray()
                        while len(collected) < len(big):
                            data, st = visa.call(
                                lib.read, sess, rng.choice((1, 13, 512, 8192))
                            )
                            if not data:
                                break
                            collected.extend(data)
                            if st == StatusCode.success:
                                break
                        if collected.decode("latin-1") != big:
                            stats.error("a chunked read reassembled incorrectly")
                    elif op == "read_stb":
                        stb = inst.read_stb()
                        if not 0 <= stb <= 0xFF:
                            stats.error(
                                "read_stb returns a plausible status byte "
                                "under the soak mix",
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
                        # transports, and that gap is reported once by the
                        # smoke suite rather than thousands of times here.
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
                        f"the {op} operation never raises under the soak mix",
                        exc,
                    )
                    # Once the connection is gone every further operation
                    # fails instantly, which buries the first failure under
                    # thousands of identical ones. Stop instead.
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
                    remaining = deadline - time.time()
                    stats.note(
                        f"{done} operations, {len(stats.failures)} failures, "
                        f"{remaining:.0f}s left"
                    )
                    last_report = time.time()

            stats.check(
                True,
                "the mixed-operation soak ran to completion",
                detail=f"{sum(tally.values())} operations",
            )
            stats.note(
                "operation mix: "
                + ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
            )
            if args.srq_thread:
                stats.note(f"{srq_count[0]} service requests handled")
                inst.disable_event(visa.SRQ, visa.HANDLER)
                inst.uninstall_handler(visa.SRQ, handler, wrapped)

            final = inst.query("*IDN?").strip()
            stats.check(
                final == idn,
                "the session is healthy at the end of the soak",
                detail=f"got {final!r}",
            )
            visa.check_errors(inst, stats, "at end of run")

            for command in ("*CLS", "*SRE 0"):
                try:
                    inst.write(command)
                except Exception:  # noqa: BLE001
                    break

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
