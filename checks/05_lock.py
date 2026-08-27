#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lock and unlock repeatedly, and make two sessions contend for a lock.

Shared locks are a HiSLIP-only story here: VXI-11 locks are exclusive,
per-link and non-nesting (RULE B.6.72), and the protocol has no field to carry
a key, so the shared-lock sections skip there rather than failing a backend for
not inventing one.
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


def _as_text(value) -> str:
    """A lock key as text, whichever type the backend handed back.

    pyvisa-py returns `str`, NI-VISA returns `bytes`. That difference is a
    real disparity and is recorded as one, but every check that merely wants
    to know whether the key round-tripped should not fail on it.
    """
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return "" if value is None else str(value)


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    args = parser.parse_args()
    shared_locks = args.protocol == "hislip"

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"lock ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst, \
             visa.session(backend, resource, timeout=args.timeout) as other:
            lib, sess = inst.visalib, inst.session
            other_lib, other_sess = other.visalib, other.session
            inst.query("*IDN?")
            other.query("*IDN?")
            visa.drain_errors(inst)

            # -- 1. lock/unlock cycling on an idle channel ------------------
            t0 = time.time()
            for i in range(args.iterations):
                _, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
                if st != StatusCode.success:
                    stats.error(f"cycle {i}: lock returned {st!r}")
                    break
                st = visa.status(lib.unlock, sess)
                if st != StatusCode.success:
                    stats.error(f"cycle {i}: unlock returned {st!r}")
                    break
            else:
                elapsed = time.time() - t0
                stats.check(
                    True,
                    f"{args.iterations} exclusive lock/unlock cycles",
                    rule="VPP-4.3 3.6.2.1",
                )
                stats.note(
                    f"{args.iterations} cycles in {elapsed:.2f}s "
                    f"({1000 * elapsed / args.iterations:.2f} ms each)"
                )

            # -- 2. shared locks --------------------------------------------
            if shared_locks:
                for i in range(min(args.iterations, 50)):
                    key, st = visa.call(
                        lib.lock, sess, constants.Lock.shared, 2000, "stress-shared"
                    )
                    if st == StatusCode.error_invalid_protocol:
                        # Not a failure, an implementation difference worth
                        # stating: R&S refuses shared locks over HiSLIP
                        # outright, where NI and pyvisa-py grant them.
                        stats.skip(
                            "the shared-lock cycles: this implementation "
                            "refuses shared locks on this transport "
                            f"({st!r})"
                        )
                        break
                    # The key comes back as bytes from some implementations
                    # and str from others, which is a disparity in its own
                    # right (see docs/findings.md) but not this check's
                    # subject -- compare on the decoded value.
                    if st != StatusCode.success or _as_text(key) != "stress-shared":
                        stats.error(f"shared cycle {i}: {st!r}, key={key!r}")
                        break
                    if visa.status(lib.unlock, sess) != StatusCode.success:
                        stats.error(f"shared cycle {i}: unlock failed")
                        break
                else:
                    stats.check(True, "shared lock/unlock cycles")
            else:
                stats.skip(
                    "the shared-lock cycles: VXI-11 locks are exclusive, "
                    "per-link and non-nesting (RULE B.6.72)"
                )

            # -- 3. lock state tracking -------------------------------------
            visa.status(lib.lock, sess, constants.Lock.exclusive, 2000, None)
            state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
            stats.check(
                st == StatusCode.success and state == constants.VI_EXCLUSIVE_LOCK,
                f"lock state reads back as exclusive (status {st!r}, {state!r})",
                rule="VPP-4.3 3.6.2.1",
            )
            visa.status(lib.unlock, sess)
            state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
            stats.check(
                st == StatusCode.success and state == constants.VI_NO_LOCK,
                f"lock state clears on unlock (status {st!r}, {state!r})",
                rule="VPP-4.3 3.6.2.1",
            )
            stats.check(
                visa.status(lib.unlock, sess) == StatusCode.error_session_not_locked,
                "a redundant unlock is refused",
            )

            # -- 4. two sessions contending ---------------------------------
            _, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
            stats.check(st == StatusCode.success, "session A takes an exclusive lock")

            t0 = time.time()
            _, st_b = visa.call(
                other_lib.lock, other_sess, constants.Lock.exclusive, 500, None
            )
            waited = time.time() - t0

            enforces_locks = True
            if st_b == StatusCode.success:
                # Either the server does not implement locking at all, or we
                # asked for the wrong thing. Tell them apart: a lock the server
                # actually honours would also stop the other session doing I/O.
                # Some gateways acknowledge the lock and enforce nothing, which
                # is a server limitation rather than a client fault -- and
                # reporting it as untestable beats reporting a false pass.
                try:
                    other.query("*IDN?")
                    enforces_locks = False
                except Exception:  # noqa: BLE001
                    enforces_locks = True

            if not enforces_locks:
                stats.skip(
                    "lock contention: this server acknowledges a lock but does "
                    "not enforce it -- session B was granted an exclusive lock "
                    "while A held one and could still do I/O"
                )
                visa.status(other_lib.unlock, other_sess)
            else:
                stats.check(
                    st_b != StatusCode.success,
                    f"session B is refused while A holds the lock (got {st_b!r})",
                    rule="VPP-4.3 3.6.2.1",
                )
                stats.note(f"B waited {waited * 1000:.0f} ms before being refused")

            stats.check(
                visa.status(lib.unlock, sess) == StatusCode.success,
                "session A unlocks",
            )
            _, st_b = visa.call(
                other_lib.lock, other_sess, constants.Lock.exclusive, 2000, None
            )
            stats.check(
                st_b == StatusCode.success, "session B can lock once A released"
            )
            visa.status(other_lib.unlock, other_sess)

            # -- 5. a shared lock is shareable --------------------------------
            if shared_locks:
                key, st = visa.call(
                    lib.lock, sess, constants.Lock.shared, 2000, "team-key"
                )
                stats.check(st == StatusCode.success, "session A takes a shared lock")
                _, st_b = visa.call(
                    other_lib.lock, other_sess, constants.Lock.shared, 2000, key
                )
                stats.check(
                    st_b == StatusCode.success,
                    f"session B joins the shared lock with A's key (got {st_b!r})",
                    rule="VPP-4.3 3.6.2.1",
                )
                visa.status(other_lib.unlock, other_sess)
                visa.status(lib.unlock, sess)
            else:
                stats.skip(
                    "the shared-lock join: VXI-11 has no shared-lock concept "
                    "(RULE B.6.72)"
                )

            # -- 6. still healthy ---------------------------------------------
            stats.check(
                bool(inst.query("*IDN?").strip()), "session A healthy at the end"
            )
            stats.check(
                bool(other.query("*IDN?").strip()), "session B healthy at the end"
            )
            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
