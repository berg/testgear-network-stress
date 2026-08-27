#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 section 3.6: what viLock and viUnlock owe the caller.

Every check here cites the clause it enforces. That is not decoration: the
vendor comparison found that of the checks three implementations disagreed
about, every single one that cited no clause turned out to be the check's own
fault, and every one that cited a clause survived. A check written from a
sentence in the spec is a different kind of object from one written from
observed behaviour.

Lock *counting* is the richest part. VPP-4.3 specifies nesting -- a session may
lock repeatedly and must unlock the matching number of times -- and that
bookkeeping lives entirely in the client, invisible on the wire. It is exactly
the kind of thing three implementations do differently.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def as_text(value) -> str:
    """A lock key as text, whichever type the backend returned.

    pyvisa-py gives `str`, NI-VISA gives `bytes`. Recorded as a disparity in
    docs/findings.md; normalised here so it does not contaminate checks whose
    subject is something else.
    """
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return "" if value is None else str(value)


def shared_locks_supported() -> bool:
    """VXI-11 has no shared-lock concept at all (RULE B.6.72)."""
    return CTX["protocol"] == "hislip"


def lock_state(inst) -> tuple:
    return visa.call(inst.visalib.get_attribute, inst.session, RA.resource_lock_state)


# ---------------------------------------------------------------------------
# Lock counting and nesting
# ---------------------------------------------------------------------------
@check("a nested exclusive lock reports VI_SUCCESS_NESTED_EXCLUSIVE",
       rule="VPP-4.3 3.6.28")
def check_exclusive_nesting():
    """3.6.28: locking exclusive again with a non-zero count returns
    VI_SUCCESS_NESTED_EXCLUSIVE -- a completion code, not an error.

    VXI-11 locks are non-nesting on the wire (RULE B.6.72), so a conforming
    client has to keep the count itself (3.6.10) rather than send a second
    device_lock. Forwarding it makes the session wait for a lock it is already
    holding, which is a deadlock against itself.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, first = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        assert first == StatusCode.success, f"the first lock returned {first!r}"
        _, second = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)

        # Unwind whatever was actually taken before asserting.
        visa.status(lib.unlock, sess)
        if second in (StatusCode.success, StatusCode.success_nested_exclusive):
            visa.status(lib.unlock, sess)

        assert second != StatusCode.error_timeout, (
            "locking exclusive twice on one session timed out: the second "
            "request went to the server, which is already holding the lock for "
            "this very session. VXI-11 locks do not nest on the wire "
            "(RULE B.6.72), so the count belongs in the client (RULE 3.6.10)"
        )
        assert second == StatusCode.success_nested_exclusive, (
            f"expected VI_SUCCESS_NESTED_EXCLUSIVE, got {second!r}"
        )


@check("the unlock that leaves a lock still held reports the nesting",
       rule="VPP-4.3 3.6.32")
def check_unlock_reports_nesting():
    """3.6.32: unlocking while the exclusive count is still non-zero returns
    VI_SUCCESS_NESTED_EXCLUSIVE, so the caller can tell "released" from
    "released one of several"."""
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, first = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        _, second = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        if second not in (StatusCode.success, StatusCode.success_nested_exclusive):
            visa.status(lib.unlock, sess)
            raise Skip(f"this implementation does not nest exclusive locks ({second!r})")

        inner = visa.status(lib.unlock, sess)
        outer = visa.status(lib.unlock, sess)
        assert inner == StatusCode.success_nested_exclusive, (
            f"the unlock leaving one lock still held returned {inner!r}, so a "
            f"caller cannot tell it from a full release"
        )
        assert outer == StatusCode.success, (
            f"the final unlock returned {outer!r}, expected VI_SUCCESS"
        )


@check("a nested shared lock reports VI_SUCCESS_NESTED_SHARED",
       rule="VPP-4.3 3.6.29")
def check_shared_nesting():
    """3.6.29, the shared-lock counterpart of 3.6.28."""
    if not shared_locks_supported():
        raise Skip("VXI-11 has no shared-lock concept (RULE B.6.72)")
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, first = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "nest")
        if first == StatusCode.error_invalid_protocol:
            raise Skip(f"this implementation refuses shared locks here ({first!r})")
        assert first == StatusCode.success, f"the first shared lock returned {first!r}"
        _, second = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "nest")

        visa.status(lib.unlock, sess)
        if second in (StatusCode.success, StatusCode.success_nested_shared):
            visa.status(lib.unlock, sess)

        assert second == StatusCode.success_nested_shared, (
            f"expected VI_SUCCESS_NESTED_SHARED, got {second!r}"
        )


@check("one unlock of two does not release the resource", rule="VPP-4.3 3.6.10")
def check_nesting_holds_resource():
    """The consequence, rather than the completion code.

    3.6.10 keeps a count per session; the count exists so that unlocking once
    after locking twice leaves the resource held. If it does not, a caller
    that wraps a locked region inside another locked region releases the
    instrument at the inner boundary while still believing it holds it -- and
    nothing says so. That is a silent correctness failure under contention,
    which is the only place it can happen and the worst place to find it.
    """
    with open_inst() as a, open_inst() as b:
        lib, sess = a.visalib, a.session
        _, first = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        assert first == StatusCode.success, f"the first lock returned {first!r}"
        _, second = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        if second not in (StatusCode.success, StatusCode.success_nested_exclusive):
            visa.status(lib.unlock, sess)
            raise Skip(f"this implementation does not nest exclusive locks ({second!r})")

        visa.status(lib.unlock, sess)  # one of two

        _, contend = visa.call(
            b.visalib.lock, b.session, constants.Lock.exclusive, 300, None
        )
        released_early = contend == StatusCode.success
        if released_early:
            visa.status(b.visalib.unlock, b.session)
        visa.status(lib.unlock, sess)

        assert not released_early, (
            "after two locks and one unlock another session acquired the "
            "resource, so the lock count is not being kept: a caller nesting a "
            "locked region inside another gives the instrument up at the inner "
            "boundary while still believing it holds it"
        )


@check("a shared re-lock with the wrong key is refused", rule="VPP-4.3 3.6.31")
def check_shared_wrong_key():
    """3.6.31: re-locking shared with a key that is not the resource's access
    key returns VI_ERROR_INV_ACCESS_KEY -- not a second lock, and not silence.
    """
    if not shared_locks_supported():
        raise Skip("VXI-11 has no shared-lock concept (RULE B.6.72)")
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, first = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "right-key")
        if first == StatusCode.error_invalid_protocol:
            raise Skip(f"this implementation refuses shared locks here ({first!r})")
        assert first == StatusCode.success, f"the first shared lock returned {first!r}"
        _, second = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "wrong-key")

        visa.status(lib.unlock, sess)
        if second in (StatusCode.success, StatusCode.success_nested_shared):
            visa.status(lib.unlock, sess)

        assert second == StatusCode.error_invalid_access_key, (
            f"re-locking shared with a different key returned {second!r}, "
            f"expected VI_ERROR_INV_ACCESS_KEY"
        )


@check("the unlock after the last one is refused", rule="VPP-4.3 3.6.10")
def check_unlock_underflow():
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
        assert visa.status(lib.unlock, sess) == StatusCode.success
        st = visa.status(lib.unlock, sess)
        assert st == StatusCode.error_session_not_locked, (
            f"unlocking an unlocked session must report "
            f"VI_ERROR_SESN_NLOCKED, got {st!r}"
        )


@check("a shared lock taken twice returns the same key", rule="VPP-4.3 3.6.20")
def check_shared_key_stable():
    """3.6.20: re-locking shared from the same session returns the same key."""
    if not shared_locks_supported():
        raise Skip("VXI-11 has no shared-lock concept (RULE B.6.72)")
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        first, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "nest-key")
        if st == StatusCode.error_invalid_protocol:
            raise Skip(f"this implementation refuses shared locks here ({st!r})")
        assert st == StatusCode.success, f"the first shared lock returned {st!r}"
        second, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "nest-key")
        # VI_SUCCESS_NESTED_SHARED is the *correct* answer here (3.6.29), not a
        # failure. Demanding plain VI_SUCCESS failed NI-VISA for implementing
        # the rule properly -- the same mistake this file made once already
        # with the exclusive case.
        assert st in (StatusCode.success, StatusCode.success_nested_shared), (
            f"the second shared lock returned {st!r}"
        )
        visa.status(lib.unlock, sess)
        visa.status(lib.unlock, sess)
        assert as_text(first) == as_text(second), (
            f"the same session locking shared twice got two different keys: "
            f"{first!r} then {second!r}"
        )


# ---------------------------------------------------------------------------
# What the parameters mean
# ---------------------------------------------------------------------------
@check("an exclusive lock ignores the requested key", rule="VPP-4.3 3.6.13")
def check_exclusive_ignores_key():
    """3.6.13 says the key is ignored; 3.6.14 says the returned key is a
    zero-length string. NI and R&S return a generated key anyway, which is
    recorded in docs/findings.md -- so this checks the half that matters:
    passing a key must not change whether the lock is granted."""
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        key, st = visa.call(
            lib.lock, sess, constants.Lock.exclusive, 2000, "ignored-key"
        )
        visa.status(lib.unlock, sess)
        assert st == StatusCode.success, (
            f"an exclusive lock with a requestedKey was refused ({st!r}); the "
            f"key is supposed to be ignored, not honoured"
        )
        return f"granted, key came back as {key!r}"


@check("an over-long shared key is refused, not truncated", rule="VPP-4.3 3.6.17")
def check_long_key_refused():
    """3.6.17: a requestedKey of 256 characters or more is an error.

    Truncating instead would be worse than refusing: two sessions with
    different long keys would silently share a lock neither asked to share.
    """
    if not shared_locks_supported():
        raise Skip("VXI-11 has no shared-lock concept (RULE B.6.72)")
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        key, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "k" * 300)
        if st == StatusCode.success:
            visa.status(lib.unlock, sess)
            raise AssertionError(
                f"a 300-character key was accepted and came back as "
                f"{as_text(key)[:40]!r} ({len(as_text(key))} chars). Truncating "
                f"lets two sessions with different keys share a lock neither "
                f"asked to share"
            )
        if st == visa.NOT_IMPLEMENTED:
            raise AssertionError("an over-long key raised instead of erroring")
        return f"refused with {st!r}"


@check("VI_TMO_IMMEDIATE gives up at once", rule="VPP-4.3 3.6.23")
def check_immediate_timeout():
    """3.6.23: with VI_TMO_IMMEDIATE a lock that cannot be had returns at once.

    "At once" is the whole content of the rule, so this is a timing assertion
    and has to be: a caller passing VI_TMO_IMMEDIATE is saying it has
    something better to do than wait.
    """
    with open_inst() as a, open_inst() as b:
        _, st = visa.call(a.visalib.lock, a.session, constants.Lock.exclusive, 2000, None)
        assert st == StatusCode.success, f"session A could not lock ({st!r})"
        try:
            started = time.time()
            _, contend = visa.call(
                b.visalib.lock,
                b.session,
                constants.Lock.exclusive,
                constants.VI_TMO_IMMEDIATE,
                None,
            )
            elapsed = time.time() - started
            if contend == StatusCode.success:
                visa.status(b.visalib.unlock, b.session)
                raise AssertionError(
                    "session B acquired a lock session A was holding"
                )
            assert elapsed < 0.5, (
                f"VI_TMO_IMMEDIATE took {elapsed:.2f}s to give up; the rule is "
                f"that it returns immediately"
            )
            return f"gave up in {elapsed * 1000:.0f}ms with {contend!r}"
        finally:
            visa.status(a.visalib.unlock, a.session)


@check("viLock waits at least its timeout before failing", rule="VPP-4.3 3.6.22")
def check_lock_waits_full_timeout():
    """3.6.22: the operation waits *at least* the timeout before erroring.

    The complement of the existing "does it wait at all" check. Returning
    early is a real fault: a caller that asked for two seconds of patience and
    got none will report a busy instrument as a broken one.
    """
    with open_inst() as a, open_inst() as b:
        _, st = visa.call(a.visalib.lock, a.session, constants.Lock.exclusive, 5000, None)
        assert st == StatusCode.success, f"session A could not lock ({st!r})"
        try:
            started = time.time()
            _, contend = visa.call(
                b.visalib.lock, b.session, constants.Lock.exclusive, 1500, None
            )
            elapsed = time.time() - started
            if contend == StatusCode.success:
                visa.status(b.visalib.unlock, b.session)
                raise AssertionError("session B acquired a lock A was holding")
            # A little slack for scheduling, but not much: the point is that
            # the wait happened.
            assert elapsed >= 1.35, (
                f"a 1500ms lock timeout gave up after {elapsed * 1000:.0f}ms"
            )
            return f"waited {elapsed * 1000:.0f}ms for a 1500ms timeout"
        finally:
            visa.status(a.visalib.unlock, a.session)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@check("closing a session releases the locks it held", rule="VPP-4.3 3.6.21")
def check_close_releases_locks():
    """3.6.21: closing a session sets both lock counts to zero.

    The failure this prevents is the nastiest kind of leak: a crashed or
    sloppy program leaves an instrument locked against everybody, and nothing
    short of restarting the server gets it back.
    """
    holder = CTX["backend"].resource_manager().open_resource(
        CTX["resource"], open_timeout=10000
    )
    holder.timeout = CTX["timeout"]
    _, st = visa.call(
        holder.visalib.lock, holder.session, constants.Lock.exclusive, 2000, None
    )
    assert st == StatusCode.success, f"could not take the lock to begin with ({st!r})"
    holder.close()  # deliberately without unlocking

    with open_inst() as other:
        _, st = visa.call(
            other.visalib.lock, other.session, constants.Lock.exclusive, 3000, None
        )
        if st == StatusCode.success:
            visa.status(other.visalib.unlock, other.session)
        assert st == StatusCode.success, (
            f"after the holding session was closed without unlocking, another "
            f"session still could not lock ({st!r}); the lock leaked and only "
            f"a server restart will clear it"
        )


@check("a lock dies with the connection that held it", rule="VXI-11 B.6.77",
       protocols=("vxi11",))
def check_lock_dies_with_connection():
    """B.6.77: locks are tied to the core channel.

    Not the same rule as 3.6.21. That one is about an orderly close; this is
    about the connection going away underneath, which is what actually happens
    when a controller loses power or its network.
    """
    if CTX.get("server") is None:
        raise Skip("needs the mock server's fault injection")
    srv = CTX["server"]

    holder = CTX["backend"].resource_manager().open_resource(
        CTX["resource"], open_timeout=10000
    )
    holder.timeout = 2000
    _, st = visa.call(
        holder.visalib.lock, holder.session, constants.Lock.exclusive, 2000, None
    )
    assert st == StatusCode.success, f"could not take the lock to begin with ({st!r})"

    # Kill the connection under it rather than closing politely.
    with srv.faults(drop_after_bytes=0):
        try:
            holder.query("*IDN?")
        except Exception:  # noqa: BLE001
            pass
    try:
        holder.close()
    except Exception:  # noqa: BLE001
        pass

    with open_inst() as other:
        _, st = visa.call(
            other.visalib.lock, other.session, constants.Lock.exclusive, 3000, None
        )
        if st == StatusCode.success:
            visa.status(other.visalib.unlock, other.session)
        assert st == StatusCode.success, (
            f"the lock outlived the connection that held it ({st!r}); B.6.77 "
            f"ties locks to the core channel precisely so a controller that "
            f"vanishes does not lock an instrument permanently"
        )


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend,
            resource=resource,
            server=srv,
            timeout=args.timeout,
            protocol=args.protocol,
        )
        stats = harness.Stats(
            f"lock semantics ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol=args.protocol)
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
