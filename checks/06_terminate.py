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
            st = visa.status(lib.terminate, sess, 0, 0)
            if st in (
                StatusCode.error_nonsupported_operation,
                StatusCode.error_nonimplemented_operation,
                visa.NOT_IMPLEMENTED,
            ):
                stats.skip("viTerminate is implemented", f"not implemented here ({st!r})")
                stats.write_outputs(args)
                return stats.finish()

            # Long enough that the read is genuinely blocked rather than
            # about to expire on its own, short enough that an implementation
            # which never aborts does not cost a full timeout per iteration.
            blocked_timeout_s = 8.0
            inst.timeout = int(blocked_timeout_s * 1000)
            durations: list[float] = []
            aborted: list = []
            unblocked: list = []

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
                st = visa.status(lib.terminate, sess, 0, 0)
                if st != StatusCode.success:
                    stats.error(
                        "viTerminate reports VI_SUCCESS",
                        detail=f"iteration {i} returned {st!r}",
                    )

                thread.join(timeout=blocked_timeout_s + 10.0)
                if thread.is_alive():
                    stats.error(
                        "viTerminate unblocks the pending read",
                        detail=f"iteration {i}: the read never returned",
                    )
                    break

                durations.append(time.time() - t0)

                # 3.5.1.1 says an implementation *should* abort, and that a
                # terminated call *should* return VI_ERROR_ABORT -- then says
                # plainly that "the specified return value is not guaranteed",
                # and adds no implementation requirements. So the status is an
                # observation, not an assertion: NI and R&S end the read with
                # VI_ERROR_TIMEOUT and are conforming. Asserting VI_ERROR_ABORT
                # here only encoded pyvisa-py's own behaviour as the standard.
                aborted.append(outcome.get("status"))
                # Whether terminate actually unblocks the read is the same
                # "should" as the status above, so this is recorded rather
                # than failed: NI returns VI_SUCCESS from viTerminate and
                # leaves the read to run its full timeout. Worth reporting --
                # a caller relying on viTerminate to cancel a blocked read
                # gets nothing on NI over HiSLIP -- but it is not a rule
                # violation, and failing it would again be treating
                # pyvisa-py's behaviour as the standard.
                if outcome["elapsed"] > blocked_timeout_s * 0.8:
                    # Stop here. Every further iteration costs a full timeout
                    # to re-learn the same fact, which turned a 30-second
                    # script into a twelve-minute one against NI.
                    unblocked.append(False)
                    stats.note(
                        f"iteration {i}: viTerminate returned success but the "
                        f"read ran its full timeout ({outcome['elapsed']:.1f}s); "
                        f"not repeating the remaining "
                        f"{args.iterations - i - 1} iterations"
                    )
                    break
                unblocked.append(True)

                # The whole point: the session is usable immediately after.
                try:
                    got = inst.query("*IDN?").strip()
                except Exception as exc:  # noqa: BLE001
                    stats.error(
                        "the session is usable again after viTerminate",
                        exc,
                        detail=f"iteration {i}",
                    )
                    break
                if got != idn:
                    stats.error(
                        "the session is usable again after viTerminate",
                        detail=f"iteration {i}: *IDN? gave {got!r}",
                    )
                    break
            else:
                stats.check(
                    True,
                    "repeated terminate/recover cycles all succeed",
                    rule="VPP-4.3 3.2.3",
                    detail=f"{args.iterations} cycles",
                )
                stats.note(
                    f"terminate + resync took {min(durations):.3f}-"
                    f"{max(durations):.3f}s "
                    f"(mean {sum(durations) / len(durations):.3f}s)"
                )
                if unblocked and not any(unblocked):
                    stats.note(
                        "viTerminate never unblocked a read on this "
                        "implementation (3.5.1.1 recommends it; it is not "
                        "a SHALL)"
                    )
                seen = {repr(x) for x in aborted}
                stats.note(
                    "the terminated read ended with "
                    + ", ".join(sorted(seen))
                    + " (3.5.1.1 prefers VI_ERROR_ABORT but does not "
                    "guarantee it)"
                )

            # Terminating an idle session is a no-op, not an error.
            inst.timeout = args.timeout
            st = visa.status(lib.terminate, sess, 0, 0)
            stats.check(st == StatusCode.success, "terminate while idle succeeds",
                        detail=f"got {st!r}")
            final = inst.query("*IDN?").strip()
            stats.check(
                final == idn,
                "the session is healthy after an idle terminate",
                detail=f"got {final!r}",
            )
            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
