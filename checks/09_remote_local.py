#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that REN, GTR and LLO move the instrument, not just return 0.

Every other test of remote/local in this suite asserts that the seven
`RENLineOperation` codes are *accepted*. That is a weak test, and it is the one
that let a server treating every code as a no-op look healthy for a long time:
the calls returned VI_SUCCESS and nothing happened.

The oracle here is throughput. A GPIB instrument in local services the bus far
more slowly than one in remote -- about 18x on an HP 34401A -- so the state can
be read back without a human at the front panel. That effect is
instrument-specific, so it is measured first and the whole matrix is skipped if
this instrument does not show one.

What cannot be checked from a client: local lockout itself. LLO disables the
front-panel LOCAL key, which no amount of bus traffic reveals. The LLO codes
are checked for the remote/local half of their meaning only, and flagged.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402

REMOTE, LOCAL = "remote", "local"

#: What each operation should leave the instrument in. A device enters remote
#: when REN is true and it is addressed to listen, so a bare `asrt` counts as
#: remote: the next query addresses it.
EXPECTED_STATE = {
    constants.RENLineOperation.deassert: LOCAL,
    constants.RENLineOperation.asrt: REMOTE,
    constants.RENLineOperation.deassert_gtl: LOCAL,
    constants.RENLineOperation.asrt_address: REMOTE,
    constants.RENLineOperation.asrt_llo: REMOTE,
    constants.RENLineOperation.asrt_address_llo: REMOTE,
}

#: `address_gtl` is GTL *without* dropping REN, so the device goes to local and
#: then returns to remote the moment it is next addressed to listen -- which is
#: what the measurement itself does. The state is real but this oracle cannot
#: see it, and claiming otherwise would be a test that fails on a correct
#: server. Contrast `deassert_gtl`, which drops REN too and therefore sticks.
NOT_OBSERVABLE = {
    constants.RENLineOperation.address_gtl: (
        "GTL leaves REN asserted, so the next addressing returns the device to "
        "remote; issuing a query to measure is that addressing"
    ),
}

#: The ones whose local-lockout half this test cannot see.
LOCKOUT_ONLY = {
    constants.RENLineOperation.asrt_llo,
    constants.RENLineOperation.asrt_address_llo,
}


def query_rate(inst, count: int) -> float:
    """Queries per second, the proxy for remote/local state."""
    start = time.time()
    for _ in range(count):
        inst.query("*IDN?")
    return count / max(time.time() - start, 1e-6)


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    parser.add_argument(
        "--samples", type=int, default=6, help="queries per throughput measurement"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=0.05,
        help="seconds to let a remote/local change take effect before "
        "measuring (default: %(default)s)",
    )
    parser.add_argument(
        "--ratio",
        type=float,
        default=3.0,
        help="how much faster remote must be than local for the throughput "
        "oracle to be trusted (default: %(default)s)",
    )
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"remote/local ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session

            def set_ren(mode) -> StatusCode:
                """Change the state, then let it settle before anyone measures.

                The settle is not padding. A remote/local change is a bus
                operation whose effect the instrument applies in its own time,
                and the oracle here is a throughput measurement that starts
                immediately afterwards -- so without it the first sample can
                straddle the transition and land on the wrong side of the
                threshold. That produced roughly one spurious failure in eight
                runs, which is the worst kind: frequent enough to erode trust
                in the suite, rare enough to look like a real intermittent bug.
                """
                st = visa.status(lib.gpib_control_ren, sess, mode)
                time.sleep(args.settle)
                return st

            inst.query("*IDN?")
            visa.drain_errors(inst)

            # VXI-11 carries only the addressed operations (B.6.13, B.6.14);
            # the unaddressed ones are legitimately refused there, so the
            # matrix is restricted to what the transport can express.
            if args.protocol == "vxi11":
                supported = {
                    constants.RENLineOperation.asrt_address,
                    constants.RENLineOperation.asrt_address_llo,
                    constants.RENLineOperation.address_gtl,
                    constants.RENLineOperation.deassert_gtl,
                }
            else:
                supported = set(constants.RENLineOperation)

            # -- 1. every code the transport carries is at least accepted ----
            for mode in constants.RENLineOperation:
                st = set_ren(mode)
                if mode in supported:
                    stats.check(
                        st == StatusCode.success,
                        f"REN {mode.name} is accepted",
                        detail=f"got {st!r}",
                    )
                else:
                    # Every implementation refuses these -- VXI-11 has no
                    # RPC for driving REN without addressing -- but they
                    # disagree about how to say so: pyvisa-py answers
                    # VI_ERROR_NSUP_OPER, NI and R&S both answer
                    # VI_ERROR_INVALID_MODE. Both are defensible refusals, so
                    # the check is that it *is* refused.
                    stats.check(
                        st in (
                            StatusCode.error_nonsupported_operation,
                            StatusCode.error_invalid_mode,
                        ),
                        f"REN {mode.name} is refused over VXI-11",
                        detail=f"got {st!r}",
                    )

            # -- 2. calibrate the oracle -------------------------------------
            set_ren(constants.RENLineOperation.asrt_address)
            remote_rate = query_rate(inst, args.samples)
            set_ren(
                constants.RENLineOperation.deassert
                if constants.RENLineOperation.deassert in supported
                else constants.RENLineOperation.deassert_gtl
            )
            local_rate = query_rate(inst, args.samples)
            set_ren(constants.RENLineOperation.asrt_address)

            ratio = remote_rate / max(local_rate, 1e-6)
            stats.note(
                f"throughput remote {remote_rate:.0f}/s vs local "
                f"{local_rate:.0f}/s ({ratio:.1f}x)"
            )

            if ratio < args.ratio:
                stats.skip(
                    "each REN mode leaves the instrument in the state it names",
                    "this instrument shows no usable throughput difference, so "
                    "the state cannot be read back from here. A native HiSLIP "
                    "instrument has no REN line and lands here",
                )
                visa.check_errors(inst, stats, "at end of run")
                stats.write_outputs(args)
                return stats.finish()

            # Anything above the geometric mean of the two is remote.
            threshold = (remote_rate * local_rate) ** 0.5

            def observed_state() -> str:
                """Best of three, because this oracle is a timing measurement.

                A single sample sitting near the threshold flips on unrelated
                load -- another process, a GC pause -- and produces a failure
                that does not reproduce, which is worse than no check at all.
                Three samples and a majority make it stable without hiding a
                state that genuinely did not change: a code that does nothing
                loses all three.
                """
                votes = [
                    REMOTE if query_rate(inst, args.samples) > threshold else LOCAL
                    for _ in range(3)
                ]
                return max(set(votes), key=votes.count)

            # -- 3. does each code do what it says? ---------------------------
            for mode, expected in EXPECTED_STATE.items():
                if mode not in supported:
                    continue
                # Start from the opposite state, so a code that does nothing at
                # all is caught rather than passing on the state its
                # predecessor left behind.
                opposite = (
                    constants.RENLineOperation.deassert_gtl
                    if expected == REMOTE
                    else constants.RENLineOperation.asrt_address
                )
                set_ren(opposite)
                before = observed_state()

                set_ren(mode)
                after = observed_state()

                note = (
                    " (local lockout itself is not observable)"
                    if mode in LOCKOUT_ONLY
                    else ""
                )
                stats.check(
                    after == expected,
                    f"{mode.name} leaves the instrument {expected}{note}",
                    detail=f"from {before}, saw {after}",
                )

            for mode, reason in NOT_OBSERVABLE.items():
                if mode not in supported:
                    continue
                st = set_ren(mode)
                stats.check(
                    st == StatusCode.success,
                    f"{mode.name} is accepted",
                    detail=f"got {st!r}",
                )
                stats.note(f"{mode.name} effect not checked: {reason}")

            # -- 4. round trip, repeatedly ------------------------------------
            # A code that works once but not when repeated is worth catching.
            flips = 0
            for _ in range(max(2, args.iterations // 50)):
                set_ren(constants.RENLineOperation.deassert_gtl)
                if observed_state() != LOCAL:
                    stats.error("repeated deassert_gtl failed to return to local")
                    break
                set_ren(constants.RENLineOperation.asrt_address)
                if observed_state() != REMOTE:
                    stats.error("repeated asrt_address failed to return to remote")
                    break
                flips += 1
            else:
                stats.check(
                    True,
                    "repeated remote/local round trips all succeed",
                    detail=f"{flips} round trips",
                )

            # -- 5. leave it in remote -----------------------------------------
            set_ren(constants.RENLineOperation.asrt_address)
            final_state = observed_state()
            stats.check(
                final_state == REMOTE,
                "the session ends in remote",
                detail=f"observed {final_state}",
            )
            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
