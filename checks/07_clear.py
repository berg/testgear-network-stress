#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Device clear, repeatedly, including while a response is half-read.

A device clear has to leave the message stream resynchronised. The check that
matters is not that the call returned -- it is that the very next query comes
back with the right bytes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402


def clear_status(stats, lib, sess, check: str, rule: str = "VPP-4.3 3.2.3"):
    """viClear's status, or None if the library raised instead of returning one.

    VPP-4.3 3.2.3 types viClear as returning a ViStatus, and a library that
    raises out of it takes the script with it: upstream pyvisa-py answers a
    device clear over HiSLIP by reading DeviceClearAcknowledge off the sync
    channel while a stale DataEnd is still queued, and the bare RuntimeError
    that follows used to end the run. Every check after it then vanished from
    the column, which reads as "not applicable" rather than as a failure.

    The failure is recorded against the section's own check name rather than
    under a name of its own, so the row lines up with the implementations that
    pass it -- both vendors do. A separate name would leave a gap in their row
    and a gap in this one, and neither would read as a disagreement. The raise
    goes in the detail, where the evidence belongs.

    Recorded once per section rather than once per iteration: the loops here
    run twenty-five times and it is the same finding each time.
    """
    try:
        return visa.status(lib.clear, sess)
    except visa.BadCall:
        # Our mistake, not the backend's. Exit 5 is where that belongs.
        raise
    except Exception as exc:  # noqa: BLE001
        stats.check(
            False,
            check,
            rule=rule,
            detail=f"viClear raised instead of returning a status: "
            f"{type(exc).__name__}: {exc}",
        )
        return None


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    parser.set_defaults(iterations=50)
    parser.add_argument(
        "--stale-query",
        default=None,
        help="a short query whose answer differs from *IDN?, used to tell a "
        "leaked response apart from a fresh one. Defaults to the mock's "
        "TEST:LINES?, or SYST:VERS? against real hardware",
    )
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"clear ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session
            idn = inst.query("*IDN?").strip()
            visa.drain_errors(inst)
            big_query = visa.resolve_big_query(args, srv, inst, stats)

            # -- 1. clear on an idle session --------------------------------
            t0 = time.time()
            for i in range(args.iterations):
                status = clear_status(
                    stats, lib, sess, "repeated clear/query cycles all succeed"
                )
                if status is None:
                    break
                if status != StatusCode.success:
                    stats.error(
                        "repeated clear/query cycles all succeed",
                        detail=f"iteration {i}: viClear failed",
                    )
                    break
                got = inst.query("*IDN?").strip()
                if got != idn:
                    stats.error(
                        "repeated clear/query cycles all succeed",
                        detail=f"iteration {i}: after clear *IDN? gave {got!r}",
                    )
                    break
            else:
                elapsed = time.time() - t0
                stats.check(
                    True,
                    "repeated clear/query cycles all succeed",
                    rule="VPP-4.3 3.2.3",
                    detail=f"{args.iterations} cycles",
                )
                stats.note(
                    f"{args.iterations} clears in {elapsed:.2f}s "
                    f"({1000 * elapsed / args.iterations:.1f} ms each)"
                )

            # -- 2. clear with a response left unread ------------------------
            # The instrument has queued a response nobody collected; the clear
            # must throw it away rather than leave it to corrupt the next read.
            # The abandoned query has to differ from the one used to check,
            # otherwise a leaked response is indistinguishable from a fresh
            # one.
            probe = args.stale_query or (
                "TEST:LINES?" if srv is not None else "SYST:VERS?"
            )
            if srv is not None:
                srv.respond("TEST:LINES?", "stale-marker")

            stale_value = None
            try:
                stale_value = inst.query(probe).strip()
            except Exception:  # noqa: BLE001
                visa.drain_errors(inst)
                stats.skip(
                    "clear discards an uncollected response",
                    f"the probe query {probe} is unsupported here",
                )

            if stale_value is not None and stale_value == idn:
                stats.skip(
                    "clear discards an uncollected response",
                    f"the probe query {probe} answers the same as *IDN?, so a "
                    f"leaked reply is indistinguishable from a fresh one",
                )
                stale_value = None

            if stale_value is not None:
                for i in range(min(args.iterations, 25)):
                    lib.write(sess, probe.encode() + b"\n")
                    status = clear_status(
                        stats, lib, sess, "clear discards an uncollected response"
                    )
                    if status is None:
                        break
                    if status != StatusCode.success:
                        stats.error(
                            "clear discards an uncollected response",
                            detail=f"iteration {i}: viClear failed",
                        )
                        break
                    got = inst.query("*IDN?").strip()
                    if got != idn:
                        stats.error(
                            "clear discards an uncollected response",
                            detail=f"iteration {i}: stale data leaked, got {got!r} "
                            f"(the abandoned {probe} response was {stale_value!r})",
                        )
                        break
                else:
                    stats.check(
                        True,
                        "clear discards an uncollected response",
                        rule="VPP-4.3 3.2.3",
                    )

            # Abandoning a query mid-flight is the point of the section above,
            # and an instrument that parses the truncated command complains
            # about it -- "Undefined header;SYST:V" and the like, which is
            # itself evidence the clear reached it. Those are self-inflicted,
            # so clear them here rather than let them drown the end-of-run
            # check for errors we did not cause.
            caused = visa.drain_errors(inst)
            if caused:
                stats.note(
                    f"{len(caused)} error(s) from the deliberately abandoned "
                    f"queries, e.g. {caused[0]}"
                )

            # -- 3. clear part-way through reading a large response -----------
            if big_query:
                reference = inst.query(big_query)
                for i in range(min(args.iterations, 25)):
                    lib.write(sess, big_query.encode() + b"\n")
                    partial, _ = visa.call(lib.read, sess, 100)  # deliberately partial
                    if partial is None or len(partial) != 100:
                        stats.error(
                            "clear resyncs mid-message",
                            detail=f"iteration {i}: the partial read got "
                            f"{0 if partial is None else len(partial)} bytes",
                        )
                        break
                    status = clear_status(
                        stats, lib, sess, "clear resyncs mid-message"
                    )
                    if status is None:
                        break
                    if status != StatusCode.success:
                        stats.error(
                            "clear resyncs mid-message",
                            detail=f"iteration {i}: viClear failed",
                        )
                        break
                    got = inst.query("*IDN?").strip()
                    if got != idn:
                        stats.error(
                            "clear resyncs mid-message",
                            detail=f"iteration {i}: stream not resynced, got {got!r}",
                        )
                        break
                else:
                    stats.check(
                        True, "clear resyncs mid-message", rule="VPP-4.3 3.2.3"
                    )

                # -- 4. the large query still works afterwards ----------------
                # Wrapped, because this is where a session left desynchronised
                # by an earlier failed clear surfaces: the query raises rather
                # than returning the wrong thing, and an unguarded call here
                # loses the end-of-run error check below.
                with stats.attempt(
                    "large reads still intact after clears"
                ) as intact:
                    if inst.query(big_query) != reference:
                        raise AssertionError(
                            "the large query no longer returns its reference "
                            "value"
                        )
                del intact

            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
