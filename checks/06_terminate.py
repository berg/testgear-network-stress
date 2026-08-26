#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancel blocked reads with viTerminate, over and over.

Each terminate has to unblock the reader *and* resynchronise the protocol, so
the check that matters is not that the call returned -- it is that the session
still works afterwards, every single time.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    parser.set_defaults(iterations=25)
    parser.add_argument(
        "--delay", type=float, default=0.3, help="seconds to wait before terminating"
    )
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"terminate ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session
            idn = inst.query("*IDN?").strip()
            visa.drain_errors(inst)

            # Probe once before committing to the loop: a backend that does
            # not implement viTerminate should cost a skip, not 25 identical
            # failures burying everything else in the file.
            st = visa.status(lib.terminate, sess, None, None)
            if st in (
                StatusCode.error_nonsupported_operation,
                visa.NOT_IMPLEMENTED,
            ):
                stats.skip(f"viTerminate is not implemented here ({st!r})")
                if args.report:
                    stats.write_report(args.report)
                return stats.finish()

            # A long timeout so the read is genuinely blocked rather than
            # about to expire on its own.
            inst.timeout = 30000
            durations: list[float] = []

            for i in range(args.iterations):
                outcome: dict = {}

                # A command that produces no response, so the read that
                # follows genuinely blocks. Without a preceding write the
                # interface short-circuits: the previous message already
                # ended, so there is nothing to wait for and the read returns
                # empty straight away.
                lib.write(sess, b"*CLS\n")

                def reader() -> None:
                    t0 = time.time()
                    data, st = visa.call(lib.read, sess, 4096)
                    outcome["elapsed"] = time.time() - t0
                    outcome["status"] = st
                    outcome["data"] = data

                thread = threading.Thread(target=reader)
                thread.start()
                time.sleep(args.delay)

                t0 = time.time()
                st = visa.status(lib.terminate, sess, None, None)
                if st != StatusCode.success:
                    stats.error(f"iteration {i}: viTerminate returned {st!r}")

                thread.join(timeout=30.0)
                if thread.is_alive():
                    stats.error(f"iteration {i}: the blocked read never returned")
                    break

                durations.append(time.time() - t0)

                if outcome.get("status") != StatusCode.error_abort:
                    stats.error(
                        f"iteration {i}: the blocked read returned "
                        f"{outcome.get('status')!r}, expected VI_ERROR_ABORT"
                    )
                    break
                if outcome["elapsed"] > 25:
                    stats.error(
                        f"iteration {i}: the read ran to timeout, not aborted"
                    )
                    break

                # The whole point: the session is usable immediately after.
                try:
                    got = inst.query("*IDN?").strip()
                except Exception as exc:  # noqa: BLE001
                    stats.error(
                        f"iteration {i}: session unusable after terminate", exc
                    )
                    break
                if got != idn:
                    stats.error(
                        f"iteration {i}: after terminate *IDN? gave {got!r}"
                    )
                    break
            else:
                stats.check(
                    True,
                    f"{args.iterations} terminate/recover cycles",
                    rule="VPP-4.3 3.2.3",
                )
                stats.note(
                    f"terminate + resync took {min(durations):.3f}-"
                    f"{max(durations):.3f}s "
                    f"(mean {sum(durations) / len(durations):.3f}s)"
                )

            # Terminating an idle session is a no-op, not an error.
            inst.timeout = args.timeout
            st = visa.status(lib.terminate, sess, None, None)
            stats.check(st == StatusCode.success, f"terminate while idle -> {st!r}")
            stats.check(
                inst.query("*IDN?").strip() == idn,
                "session healthy after an idle terminate",
            )
            visa.check_errors(inst, stats, "at end of run")

        if args.report:
            stats.write_report(args.report)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
