#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancel blocked reads with viTerminate, over and over.

Each terminate has to unblock the reader *and* resynchronise the protocol, so
the check that matters is not that the call returned -- it is that the session
still works afterwards, every single time.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}

#: Statuses that mean the operation is simply absent here.
UNIMPLEMENTED = (
    StatusCode.error_nonsupported_operation,
    StatusCode.error_nonimplemented_operation,
    visa.NOT_IMPLEMENTED,
)

#: Long enough that the read is genuinely blocked rather than about to expire
#: on its own, short enough that an implementation which never aborts does not
#: cost a full timeout per iteration.
BLOCKED_TIMEOUT_S = 8.0


def add_arguments(parser) -> None:
    parser.set_defaults(iterations=25)
    parser.add_argument(
        "--delay", type=float, default=0.3, help="seconds to wait before terminating"
    )


def io():
    return CTX["session"].visalib, CTX["session"].session


def require_terminate() -> None:
    """Skip, rather than fail, where viTerminate does not exist.

    Probed once and remembered. All three checks in this file call it, so a
    backend without viTerminate skips all three *under their own names* --
    rather than reporting one row of its own, which would leave three dashes
    here and one in every other column.
    """
    if "implemented" not in STATE:
        lib, sess = io()
        st = visa.status(lib.terminate, sess, 0, 0)
        STATE["implemented"] = st not in UNIMPLEMENTED
        STATE["probe_status"] = st
    if not STATE["implemented"]:
        raise Skip(
            f"viTerminate is not implemented here ({STATE['probe_status']!r})"
        )


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        STATE["idn"] = session.query("*IDN?").strip()
        visa.drain_errors(session)
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")


@check("repeated terminate/recover cycles all succeed", rule="VPP-4.3 3.2.3")
def check_terminate_cycles():
    """Terminate a blocked read `--iterations` times, resynchronising between.

    The failures found along the way are recorded under their own names as
    they happen -- viTerminate not reporting success, the read never
    returning, the session not coming back -- because they are separate
    findings and a caller hitting one wants to know which. This check is the
    verdict on the cycles themselves, and it stops at the first of them: there
    is nothing to learn from twenty-four more iterations of a broken one.
    """
    require_terminate()
    args, stats = CTX["args"], CTX["stats"]
    inst, (lib, sess) = CTX["session"], io()
    inst.timeout = int(BLOCKED_TIMEOUT_S * 1000)
    durations: list[float] = []
    aborted: list = []
    unblocked: list[bool] = []
    try:
        for i in range(args.iterations):
            outcome: dict = {}

            # A command that produces no response, so the read that follows
            # genuinely blocks. Without a preceding write the interface
            # short-circuits: the previous message already ended, so there is
            # nothing to wait for and the read returns empty straight away.
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

            thread.join(timeout=BLOCKED_TIMEOUT_S + 10.0)
            if thread.is_alive():
                stats.error(
                    "viTerminate unblocks the pending read",
                    detail=f"iteration {i}: the read never returned",
                )
                raise Skip(
                    f"the read on iteration {i} never returned, so the "
                    f"remaining cycles could not be run"
                )

            durations.append(time.time() - t0)

            # 3.5.1.1 says an implementation *should* abort, and that a
            # terminated call *should* return VI_ERROR_ABORT -- then says
            # plainly that "the specified return value is not guaranteed", and
            # adds no implementation requirements. So the status is an
            # observation, not an assertion: NI and R&S end the read with
            # VI_ERROR_TIMEOUT and are conforming. Asserting VI_ERROR_ABORT
            # here only encoded pyvisa-py's own behaviour as the standard.
            aborted.append(outcome.get("status"))
            # Whether terminate actually unblocks the read is the same
            # "should" as the status above, so this is recorded rather than
            # failed: NI returns VI_SUCCESS from viTerminate and leaves the
            # read to run its full timeout. Worth reporting -- a caller
            # relying on viTerminate to cancel a blocked read gets nothing on
            # NI over HiSLIP -- but it is not a rule violation, and failing it
            # would again be treating pyvisa-py's behaviour as the standard.
            if outcome["elapsed"] > BLOCKED_TIMEOUT_S * 0.8:
                unblocked.append(False)
                # A skip carrying why, not a failure and not silence. The
                # cycles genuinely did not run to completion; failing them
                # would make a non-violation look like one, and recording
                # nothing published a blank cell for the one check this whole
                # file exists to make.
                raise Skip(
                    f"viTerminate returned success but left the read to run "
                    f"its full timeout ({outcome['elapsed']:.1f}s) on "
                    f"iteration {i}. 3.5.1.1 recommends aborting and does not "
                    f"require it, so this is not a failure -- but the "
                    f"remaining {args.iterations - i - 1} iterations would "
                    f"each cost another full timeout to re-learn it"
                )
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
                raise Skip(
                    f"the session did not come back on iteration {i}, so the "
                    f"remaining cycles could not be run"
                ) from exc
            if got != STATE["idn"]:
                stats.error(
                    "the session is usable again after viTerminate",
                    detail=f"iteration {i}: *IDN? gave {got!r}",
                )
                raise Skip(
                    f"the session answered {got!r} on iteration {i}, so the "
                    f"remaining cycles could not be run"
                )
    finally:
        # Whatever happened, the checks after this one expect the session's
        # configured timeout, not the long one the blocked reads needed.
        inst.timeout = CTX["timeout"]
        if durations:
            stats.note(
                f"terminate + resync took {min(durations):.3f}-"
                f"{max(durations):.3f}s "
                f"(mean {sum(durations) / len(durations):.3f}s)"
            )
        if unblocked and not any(unblocked):
            stats.note(
                "viTerminate never unblocked a read on this implementation "
                "(3.5.1.1 recommends it; it is not a SHALL)"
            )
        if aborted:
            stats.note(
                "the terminated read ended with "
                + ", ".join(sorted({repr(x) for x in aborted}))
                + " (3.5.1.1 prefers VI_ERROR_ABORT but does not guarantee it)"
            )
    return f"{args.iterations} cycles"


@check("terminate while idle succeeds")
def check_terminate_idle():
    """Terminating an idle session is a no-op, not an error."""
    require_terminate()
    lib, sess = io()
    st = visa.status(lib.terminate, sess, 0, 0)
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("the session is healthy after an idle terminate")
def check_healthy_after_idle_terminate():
    require_terminate()
    final = CTX["session"].query("*IDN?").strip()
    assert final == STATE["idn"], f"got {final!r}"
    return f"got {final!r}"


if __name__ == "__main__":
    script.run()
