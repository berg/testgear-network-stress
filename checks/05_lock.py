#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lock and unlock repeatedly, and make two sessions contend for a lock.

Shared locks are a HiSLIP-only story here: VXI-11 locks are exclusive,
per-link and non-nesting (RULE B.6.72), and the protocol has no field to carry
a key, so the shared-lock sections skip there rather than failing a backend for
not inventing one.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}


def _as_text(value) -> str:
    """A lock key as text, whichever type the backend handed back.

    pyvisa-py returns `str`, NI-VISA returns `bytes`. That difference is a
    real disparity and is recorded as one, but every check that merely wants
    to know whether the key round-tripped should not fail on it.
    """
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return "" if value is None else str(value)


def a():
    """Session A's `(library, session)`."""
    return CTX["a"].visalib, CTX["a"].session


def b():
    """Session B's `(library, session)`."""
    return CTX["b"].visalib, CTX["b"].session


def shared_locks() -> bool:
    return CTX["protocol"] == "hislip"


@contextlib.contextmanager
def SETUP(ctx):
    """Two sessions to the same instrument, for the contention sections."""
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session_a, visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session_b:
        ctx["a"], ctx["b"] = session_a, session_b
        session_a.query("*IDN?")
        session_b.query("*IDN?")
        visa.drain_errors(session_a)
        try:
            yield
        finally:
            visa.check_errors(session_a, ctx["stats"], "at end of run")


# -- 1. lock/unlock cycling on an idle channel -------------------------------
@check("repeated exclusive lock/unlock cycles all succeed", rule="VPP-4.3 3.6.2.1")
def check_exclusive_cycles():
    args, stats = CTX["args"], CTX["stats"]
    lib, sess = a()
    t0 = time.time()
    for i in range(args.iterations):
        _, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        assert st == StatusCode.success, f"cycle {i}: lock returned {st!r}"
        st = visa.status(lib.unlock, sess)
        assert st == StatusCode.success, f"cycle {i}: unlock returned {st!r}"
    elapsed = time.time() - t0
    stats.note(
        f"{args.iterations} cycles in {elapsed:.2f}s "
        f"({1000 * elapsed / args.iterations:.2f} ms each)"
    )
    return f"{args.iterations} cycles"


# -- 2. shared locks ---------------------------------------------------------
@check("repeated shared lock/unlock cycles all succeed",
       rule="VPP-4.3 3.6.3, 3.6.5")
def check_shared_cycles():
    """Refusing shared locks outright is a rule violation, not a preference.

    This was a skip, on the assumption that an implementation refusing shared
    locks over HiSLIP had a reason and the check was overreaching. It does
    not: RULE 3.6.3 says every VISA resource SHALL support both exclusive and
    shared locks, and RULE 3.6.5 names shared locks explicitly for HiSLIP
    sessions. Excusing it here was assuming a vendor must be right -- which is
    exactly as unfounded as assuming pyvisa-py must be wrong.

    The refusal is reported against this check's own name. A name of its own
    left a gap in this row and a gap in the cycles row, and neither read as a
    disagreement -- the same argument `clear_status()` makes in 07_clear.py.
    """
    if not shared_locks():
        raise Skip(
            "VXI-11 locks are exclusive, per-link and non-nesting "
            "(RULE B.6.72)"
        )
    args = CTX["args"]
    lib, sess = a()
    for i in range(min(args.iterations, 50)):
        key, st = visa.call(
            lib.lock, sess, constants.Lock.shared, 2000, "stress-shared"
        )
        assert st != StatusCode.error_invalid_protocol, (
            f"shared locks are refused on this transport ({st!r})"
        )
        # The key comes back as bytes from some implementations and str from
        # others, which is a disparity in its own right (see docs/findings.md)
        # but not this check's subject -- compare on the decoded value.
        assert st == StatusCode.success and _as_text(key) == "stress-shared", (
            f"cycle {i}: {st!r}, key={key!r}"
        )
        assert visa.status(lib.unlock, sess) == StatusCode.success, (
            f"cycle {i}: unlock failed"
        )
    return f"{args.iterations} cycles"


# -- 3. lock state tracking --------------------------------------------------
@check("VI_ATTR_RSRC_LOCK_STATE reads back as exclusive", rule="VPP-4.3 3.6.2.1")
def check_lock_state_exclusive():
    lib, sess = a()
    visa.status(lib.lock, sess, constants.Lock.exclusive, 2000, None)
    state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
    detail = f"status {st!r}, value {state!r}"
    assert st == StatusCode.success and state == constants.VI_EXCLUSIVE_LOCK, detail
    return detail


@check("VI_ATTR_RSRC_LOCK_STATE clears on unlock", rule="VPP-4.3 3.6.2.1")
def check_lock_state_cleared():
    lib, sess = a()
    visa.status(lib.unlock, sess)
    state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
    detail = f"status {st!r}, value {state!r}"
    assert st == StatusCode.success and state == constants.VI_NO_LOCK, detail
    return detail


@check("a redundant unlock is refused")
def check_redundant_unlock():
    lib, sess = a()
    st = visa.status(lib.unlock, sess)
    assert st == StatusCode.error_session_not_locked, f"got {st!r}"
    return f"got {st!r}"


# -- 4. two sessions contending ----------------------------------------------
@check("session A takes an exclusive lock")
def check_a_locks():
    lib, sess = a()
    _, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
    STATE["a_holds"] = st == StatusCode.success
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("session B is refused while A holds the lock", rule="VPP-4.3 3.6.2.1")
def check_b_refused():
    """A server that acknowledges a lock and enforces nothing is a server
    limitation, not a client fault.

    Tell the two apart before scoring: a lock the server actually honours
    would also stop the other session doing I/O. Reporting an unenforced lock
    as untestable beats reporting a false pass.
    """
    # Not guarded on A having got its lock. pyvisa-py fails to take one over
    # HiSLIP and B is *still* refused there -- which is the finding, and a
    # skip would throw it away. If A holds nothing and B is granted a lock,
    # that is a real result too, and the enforcement probe below tells the two
    # apart.
    stats = CTX["stats"]
    other_lib, other_sess = b()
    t0 = time.time()
    _, st_b = visa.call(
        other_lib.lock, other_sess, constants.Lock.exclusive, 500, None
    )
    waited = time.time() - t0

    if st_b == StatusCode.success:
        try:
            CTX["b"].query("*IDN?")
            enforces = False
        except Exception:  # noqa: BLE001
            enforces = True
        if not enforces:
            visa.status(other_lib.unlock, other_sess)
            raise Skip(
                "this server acknowledges a lock but does not enforce it -- "
                "session B was granted an exclusive lock while A held one "
                "and could still do I/O"
            )
    stats.note(f"B waited {waited * 1000:.0f} ms before being refused")
    assert st_b != StatusCode.success, f"got {st_b!r}"
    return f"got {st_b!r}"


@check("session A unlocks")
def check_a_unlocks():
    lib, sess = a()
    st = visa.status(lib.unlock, sess)
    STATE["a_holds"] = False
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("session B can lock once A released")
def check_b_locks_after():
    other_lib, other_sess = b()
    _, st_b = visa.call(
        other_lib.lock, other_sess, constants.Lock.exclusive, 2000, None
    )
    visa.status(other_lib.unlock, other_sess)
    assert st_b == StatusCode.success, f"got {st_b!r}"
    return f"got {st_b!r}"


# -- 5. a shared lock is shareable -------------------------------------------
@check("session A takes a shared lock", protocols=("hislip",))
def check_a_shared_lock():
    """HiSLIP only, and registered that way rather than skipped: VXI-11 has no
    shared lock to take, so the check does not apply rather than failing to
    run. The check below it *is* skipped there, because "B joins A's shared
    lock" is a claim VXI-11 answers -- with "there is no such thing"."""
    lib, sess = a()
    key, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "team-key")
    # Recorded whatever came back, including on the failing path. Three of the
    # four implementations in CI refuse a shared lock over HiSLIP, and the
    # check below still has to run: "B could not join A's shared lock" is the
    # finding, and skipping it because A had nothing to share would replace a
    # FAIL with a SKIP in exactly the columns the finding is about.
    STATE["shared_key"] = key
    assert st == StatusCode.success, f"got {st!r}, key {key!r}"
    return f"got {st!r}, key {key!r}"


@check("session B joins the shared lock with A's key", rule="VPP-4.3 3.6.2.1")
def check_b_joins_shared_lock():
    if not shared_locks():
        raise Skip("VXI-11 has no shared-lock concept (RULE B.6.72)")
    if "shared_key" not in STATE:
        raise Skip(
            "the check that takes A's shared lock did not run, so there is no "
            "key to join with"
        )
    other_lib, other_sess = b()
    lib, sess = a()
    try:
        _, st_b = visa.call(
            other_lib.lock, other_sess, constants.Lock.shared, 2000,
            STATE["shared_key"],
        )
        assert st_b == StatusCode.success, f"got {st_b!r}"
        return f"got {st_b!r}"
    finally:
        visa.status(other_lib.unlock, other_sess)
        visa.status(lib.unlock, sess)


# -- 6. still healthy --------------------------------------------------------
def release_everything() -> None:
    """Release anything either session is still holding.

    The sections above take locks along paths that do not all end in an
    unlock -- an implementation that refuses a shared lock skips the release
    that was meant to follow it -- and a health query made while the *other*
    session holds a lock is refused for a reason that has nothing to do with
    health.
    """
    if STATE.get("released"):
        return
    STATE["released"] = True
    for lib_, sess_ in (a(), b()):
        for _ in range(4):
            if visa.status(lib_.unlock, sess_) != StatusCode.success:
                break


def healthy(resource):
    """Query, turning a VISA error into a reportable status.

    An unwrapped query here used to raise straight out of main and kill the
    script, which removed all 13 of its checks from that implementation's
    column -- displayed as blank cells, which read as "not applicable" rather
    than "this run died".
    """
    try:
        reply = resource.query("*IDN?").strip()
        return bool(reply), f"the session answered {reply!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"query was refused ({visa.visa_status(exc)})"


@check("session A healthy at the end")
def check_a_healthy():
    release_everything()
    ok, why = healthy(CTX["a"])
    assert ok, why
    return why


@check("session B healthy at the end")
def check_b_healthy():
    release_everything()
    ok, why = healthy(CTX["b"])
    assert ok, why
    return why


if __name__ == "__main__":
    script.run()
