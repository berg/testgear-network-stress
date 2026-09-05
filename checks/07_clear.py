#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Device clear, repeatedly, including while a response is half-read.

A device clear has to leave the message stream resynchronised. The check that
matters is not that the call returned -- it is that the very next query comes
back with the right bytes.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}


def add_arguments(parser) -> None:
    parser.set_defaults(iterations=50)
    parser.add_argument(
        "--stale-query",
        default=None,
        help="a short query whose answer differs from *IDN?, used to tell a "
        "leaked response apart from a fresh one. Defaults to the mock's "
        "TEST:LINES?, or SYST:VERS? against real hardware",
    )


def io():
    return CTX["session"].visalib, CTX["session"].session


def cycles() -> int:
    """How many times the sections after the first repeat."""
    return min(CTX["args"].iterations, 25)


def clear_status():
    """viClear's status, raising an assertion if the library raised instead.

    VPP-4.3 3.2.3 types viClear as returning a ViStatus, and a library that
    raises out of it takes the script with it: upstream pyvisa-py answers a
    device clear over HiSLIP by reading DeviceClearAcknowledge off the sync
    channel while a stale DataEnd is still queued, and the bare RuntimeError
    that follows used to end the run. Every check after it then vanished from
    the column, which reads as "not applicable" rather than as a failure.

    Turning the raise into an assertion means it is recorded against whichever
    section was running rather than under a name of its own, so the row lines
    up with the implementations that pass it -- both vendors do. A separate
    name would leave a gap in their row and a gap in this one, and neither
    would read as a disagreement. The raise goes in the detail, where the
    evidence belongs.
    """
    lib, sess = io()
    try:
        return visa.status(lib.clear, sess)
    except visa.BadCall:
        # Our mistake, not the backend's. Exit 5 is where that belongs.
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"viClear raised instead of returning a status: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def reference() -> str:
    """The large response, read once and remembered.

    Its own memo rather than a value the mid-message check leaves behind: that
    check fails on pyvisa-py over HiSLIP, and hanging the check after it on
    whether it got far enough would turn a FAIL into a SKIP in the one column
    the finding is about.
    """
    if "reference" not in STATE:
        STATE["reference"] = CTX["session"].query(STATE["big_query"])
    return STATE["reference"]


def clear_and_requery(i: int, what: str) -> None:
    """One clear, then the query that proves the stream resynchronised."""
    status = clear_status()
    assert status == StatusCode.success, f"iteration {i}: viClear failed"
    got = CTX["session"].query("*IDN?").strip()
    assert got == STATE["idn"], f"iteration {i}: {what}, got {got!r}"


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        STATE["idn"] = session.query("*IDN?").strip()
        visa.drain_errors(session)
        STATE["big_query"] = visa.resolve_big_query(
            ctx["args"], ctx["server"], session, ctx["stats"]
        )
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")


@check("repeated clear/query cycles all succeed", rule="VPP-4.3 3.2.3")
def check_clear_cycles():
    """Clear an idle session over and over, querying between each."""
    args, stats = CTX["args"], CTX["stats"]
    t0 = time.time()
    for i in range(args.iterations):
        clear_and_requery(i, "after clear *IDN? disagreed")
    elapsed = time.time() - t0
    stats.note(
        f"{args.iterations} clears in {elapsed:.2f}s "
        f"({1000 * elapsed / args.iterations:.1f} ms each)"
    )
    return f"{args.iterations} cycles"


@check("clear discards an uncollected response", rule="VPP-4.3 3.2.3")
def check_clear_discards_response():
    """The instrument has queued a response nobody collected; the clear must
    throw it away rather than leave it to corrupt the next read.

    The abandoned query has to differ from the one used to check, otherwise a
    leaked response is indistinguishable from a fresh one -- which is what the
    two skips below are about.
    """
    args, stats, inst = CTX["args"], CTX["stats"], CTX["session"]
    lib, sess = io()
    probe = args.stale_query or (
        "TEST:LINES?" if CTX["server"] is not None else "SYST:VERS?"
    )
    if CTX["server"] is not None:
        CTX["server"].respond("TEST:LINES?", "stale-marker")

    try:
        stale_value = inst.query(probe).strip()
    except Exception as exc:  # noqa: BLE001
        visa.drain_errors(inst)
        raise Skip(f"the probe query {probe} is unsupported here") from exc
    if stale_value == STATE["idn"]:
        raise Skip(
            f"the probe query {probe} answers the same as *IDN?, so a leaked "
            f"reply is indistinguishable from a fresh one"
        )

    try:
        for i in range(cycles()):
            lib.write(sess, probe.encode() + b"\n")
            clear_and_requery(
                i,
                f"stale data leaked (the abandoned {probe} response was "
                f"{stale_value!r})",
            )
    finally:
        # Abandoning a query mid-flight is the point of this section, and an
        # instrument that parses the truncated command complains about it --
        # "Undefined header;SYST:V" and the like, which is itself evidence the
        # clear reached it. Those are self-inflicted, so clear them here rather
        # than let them drown the end-of-run check for errors we did not cause.
        caused = visa.drain_errors(inst)
        if caused:
            stats.note(
                f"{len(caused)} error(s) from the deliberately abandoned "
                f"queries, e.g. {caused[0]}"
            )
    return f"{cycles()} cycles, each abandoning a {probe} response"


@check("clear resyncs mid-message", rule="VPP-4.3 3.2.3")
def check_clear_mid_message():
    """Clear part-way through reading a large response."""
    if not STATE["big_query"]:
        raise Skip(
            "there is no usable large-response query here, so no read can be "
            "interrupted part-way"
        )
    lib, sess = io()
    reference()
    for i in range(cycles()):
        lib.write(sess, STATE["big_query"].encode() + b"\n")
        partial, _ = visa.call(lib.read, sess, 100)  # deliberately partial
        assert partial is not None and len(partial) == 100, (
            f"iteration {i}: the partial read got "
            f"{0 if partial is None else len(partial)} bytes"
        )
        clear_and_requery(i, "stream not resynced")
    return (
        f"{cycles()} cycles, each clearing 100B into a "
        f"{len(STATE['reference'])}B response"
    )


@check("large reads still intact after clears")
def check_large_reads_intact():
    """Where a session left desynchronised by an earlier failed clear
    surfaces: the query raises rather than returning the wrong thing."""
    if not STATE["big_query"]:
        raise Skip("there is no usable large-response query here")
    expected = reference()
    assert CTX["session"].query(STATE["big_query"]) == expected, (
        "the large query no longer returns its reference value"
    )
    return f"the {len(expected)}B response still matches"


if __name__ == "__main__":
    script.run()
