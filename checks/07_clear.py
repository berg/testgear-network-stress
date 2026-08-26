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
                if visa.status(lib.clear, sess) != StatusCode.success:
                    stats.error(f"iteration {i}: viClear failed")
                    break
                got = inst.query("*IDN?").strip()
                if got != idn:
                    stats.error(f"iteration {i}: after clear *IDN? gave {got!r}")
                    break
            else:
                elapsed = time.time() - t0
                stats.check(
                    True,
                    f"{args.iterations} clear/query cycles",
                    rule="VPP-4.3 3.2.3",
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
                stats.skip(f"the stale-response check: {probe} is unsupported here")

            if stale_value is not None and stale_value == idn:
                stats.skip(
                    f"the stale-response check: {probe} answers the same as "
                    f"*IDN?, so a leaked reply is indistinguishable from a "
                    f"fresh one"
                )
                stale_value = None

            if stale_value is not None:
                for i in range(min(args.iterations, 25)):
                    lib.write(sess, probe.encode() + b"\n")
                    if visa.status(lib.clear, sess) != StatusCode.success:
                        stats.error(f"unread-response iteration {i}: viClear failed")
                        break
                    got = inst.query("*IDN?").strip()
                    if got != idn:
                        stats.error(
                            f"unread-response iteration {i}: stale data leaked, "
                            f"got {got!r} (the abandoned {probe} response was "
                            f"{stale_value!r})"
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
                            f"partial-read iteration {i}: got "
                            f"{0 if partial is None else len(partial)} bytes"
                        )
                        break
                    if visa.status(lib.clear, sess) != StatusCode.success:
                        stats.error(f"partial-read iteration {i}: viClear failed")
                        break
                    got = inst.query("*IDN?").strip()
                    if got != idn:
                        stats.error(
                            f"partial-read iteration {i}: stream not resynced, "
                            f"got {got!r}"
                        )
                        break
                else:
                    stats.check(
                        True, "clear resyncs mid-message", rule="VPP-4.3 3.2.3"
                    )

                # -- 4. the large query still works afterwards ----------------
                stats.check(
                    inst.query(big_query) == reference,
                    "large reads still intact after clears",
                )

            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
