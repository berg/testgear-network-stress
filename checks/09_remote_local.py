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

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import harness, script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}

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

#: VXI-11 carries only the addressed operations (B.6.13, B.6.14); the
#: unaddressed ones are legitimately refused there, so the matrix is restricted
#: to what the transport can express.
VXI11_MODES = frozenset(
    {
        constants.RENLineOperation.asrt_address,
        constants.RENLineOperation.asrt_address_llo,
        constants.RENLineOperation.address_gtl,
        constants.RENLineOperation.deassert_gtl,
    }
)


def add_arguments(parser) -> None:
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


def protocols_for(mode) -> tuple[str, ...]:
    """Which transports can express `mode` at all."""
    return ("vxi11", "hislip") if mode in VXI11_MODES else ("hislip",)


def state_check_name(mode) -> str:
    """The name the state check for `mode` is recorded under."""
    note = (
        " (local lockout itself is not observable)" if mode in LOCKOUT_ONLY else ""
    )
    return f"{mode.name} leaves the instrument {EXPECTED_STATE[mode]}{note}"


def set_ren(mode) -> StatusCode:
    """Change the state, then let it settle before anyone measures.

    The settle is not padding. A remote/local change is a bus operation whose
    effect the instrument applies in its own time, and the oracle here is a
    throughput measurement that starts immediately afterwards -- so without it
    the first sample can straddle the transition and land on the wrong side of
    the threshold. That produced roughly one spurious failure in eight runs,
    which is the worst kind: frequent enough to erode trust in the suite, rare
    enough to look like a real intermittent bug.
    """
    lib, sess = CTX["session"].visalib, CTX["session"].session
    st = visa.status(lib.gpib_control_ren, sess, mode)
    time.sleep(CTX["args"].settle)
    return st


def query_rate(count: int | None = None) -> float:
    """Queries per second, the proxy for remote/local state."""
    count = count or CTX["args"].samples
    inst = CTX["session"]
    start = time.time()
    for _ in range(count):
        inst.query("*IDN?")
    return count / max(time.time() - start, 1e-6)


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        session.query("*IDN?")
        visa.drain_errors(session)
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")


# -- 1. every code the transport carries is at least accepted ----------------
def _ren_accepted(mode):
    def run():
        st = set_ren(mode)
        assert st == StatusCode.success, f"got {st!r}"
        return f"got {st!r}"

    return run


def _ren_refused(mode):
    def run():
        """Every implementation refuses these -- VXI-11 has no RPC for driving
        REN without addressing -- but they disagree about how to say so:
        pyvisa-py answers VI_ERROR_NSUP_OPER, NI and R&S both answer
        VI_ERROR_INVALID_MODE. Both are defensible refusals, so the check is
        that it *is* refused."""
        st = set_ren(mode)
        assert st in (
            StatusCode.error_nonsupported_operation,
            StatusCode.error_invalid_mode,
        ), f"got {st!r}"
        return f"got {st!r}"

    return run


def _register_acceptance_checks() -> None:
    add = harness.registrar(globals())
    for mode in constants.RENLineOperation:
        add(
            _ren_accepted(mode),
            f"REN {mode.name} is accepted",
            protocols=protocols_for(mode),
        )
        if mode not in VXI11_MODES:
            add(
                _ren_refused(mode),
                f"REN {mode.name} is refused over VXI-11",
                protocols=("vxi11",),
            )


_register_acceptance_checks()


# -- 2. calibrate the oracle -------------------------------------------------
def threshold() -> float:
    """Measure remote and local throughput once, and return the dividing line.

    Everything below this point depends on the measurement, so it happens on
    the first check that needs it and the rest read what it found. An
    instrument with no usable difference skips them all, under their own
    names -- a native HiSLIP instrument has no REN line and lands there.
    """
    if "threshold" not in STATE:
        args, stats = CTX["args"], CTX["stats"]
        set_ren(constants.RENLineOperation.asrt_address)
        remote_rate = query_rate()
        set_ren(
            constants.RENLineOperation.deassert
            if constants.RENLineOperation.deassert not in VXI11_MODES
            and CTX["protocol"] == "hislip"
            else constants.RENLineOperation.deassert_gtl
        )
        local_rate = query_rate()
        set_ren(constants.RENLineOperation.asrt_address)
        ratio = remote_rate / max(local_rate, 1e-6)
        stats.note(
            f"throughput remote {remote_rate:.0f}/s vs local "
            f"{local_rate:.0f}/s ({ratio:.1f}x)"
        )
        # Anything above the geometric mean of the two is remote.
        STATE["threshold"] = (
            (remote_rate * local_rate) ** 0.5 if ratio >= args.ratio else None
        )
    if STATE["threshold"] is None:
        raise Skip(
            "this instrument shows no usable throughput difference, so the "
            "state cannot be read back from here. A native HiSLIP instrument "
            "has no REN line and lands here"
        )
    return STATE["threshold"]


def observed_state() -> str:
    """Best of three, because this oracle is a timing measurement.

    A single sample sitting near the threshold flips on unrelated load --
    another process, a GC pause -- and produces a failure that does not
    reproduce, which is worse than no check at all. Three samples and a
    majority make it stable without hiding a state that genuinely did not
    change: a code that does nothing loses all three.
    """
    line = threshold()
    votes = [REMOTE if query_rate() > line else LOCAL for _ in range(3)]
    return max(set(votes), key=votes.count)


# -- 3. does each code do what it says? --------------------------------------
def _state_check(mode, expected):
    def run():
        # Start from the opposite state, so a code that does nothing at all is
        # caught rather than passing on the state its predecessor left behind.
        threshold()
        opposite = (
            constants.RENLineOperation.deassert_gtl
            if expected == REMOTE
            else constants.RENLineOperation.asrt_address
        )
        set_ren(opposite)
        before = observed_state()
        set_ren(mode)
        after = observed_state()
        detail = f"from {before}, saw {after}"
        assert after == expected, detail
        return detail

    return run


def _not_observable_check(mode, reason):
    def run():
        threshold()
        st = set_ren(mode)
        CTX["stats"].note(f"{mode.name} effect not checked: {reason}")
        assert st == StatusCode.success, f"got {st!r}"
        return f"got {st!r}"

    return run


def _register_state_checks() -> None:
    add = harness.registrar(globals())
    for mode, expected in EXPECTED_STATE.items():
        add(
            _state_check(mode, expected),
            state_check_name(mode),
            protocols=protocols_for(mode),
        )
    for mode, reason in NOT_OBSERVABLE.items():
        add(
            _not_observable_check(mode, reason),
            f"{mode.name} is accepted",
            protocols=protocols_for(mode),
        )


_register_state_checks()


# -- 4. round trip, repeatedly -----------------------------------------------
@check("repeated remote/local round trips all succeed")
def check_round_trips():
    """A code that works once but not when repeated is worth catching."""
    threshold()
    flips = 0
    for _ in range(max(2, CTX["args"].iterations // 50)):
        set_ren(constants.RENLineOperation.deassert_gtl)
        assert observed_state() == LOCAL, (
            "repeated deassert_gtl failed to return to local"
        )
        set_ren(constants.RENLineOperation.asrt_address)
        assert observed_state() == REMOTE, (
            "repeated asrt_address failed to return to remote"
        )
        flips += 1
    return f"{flips} round trips"


# -- 5. leave it in remote ---------------------------------------------------
@check("the session ends in remote")
def check_ends_in_remote():
    threshold()
    set_ren(constants.RENLineOperation.asrt_address)
    final_state = observed_state()
    assert final_state == REMOTE, f"observed {final_state}"
    return f"observed {final_state}"


if __name__ == "__main__":
    script.run(title="remote/local")
