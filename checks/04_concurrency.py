#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Concurrency: many threads on one session, and many sessions at once.

The HiSLIP synchronous channel is single-threaded by design -- one write then
one read -- so this drives synchronous traffic from a single thread while other
threads hammer the asynchronous channel (status queries, locks, remote/local).
That is the arrangement the demultiplexer has to survive, and it is what an SRQ
handler doing a status query looks like in practice.

Also checks that closing a session reaps its threads and its sockets.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402


def ren_mode(protocol: str) -> constants.RENLineOperation:
    """A remote/local operation the transport actually carries.

    VXI-11 has no RPC for driving REN without addressing (B.6.13), so the
    unaddressed assert is refused there and would report a fault on every
    iteration of a worker whose job is to make unrelated traffic.
    """
    if protocol == "vxi11":
        return constants.RENLineOperation.asrt_address
    return constants.RENLineOperation.asrt


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    parser.add_argument("--threads", type=int, default=4, help="async worker threads")
    parser.add_argument("--sessions", type=int, default=6, help="parallel sessions")
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"concurrency ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        open_session = lambda: visa.session(  # noqa: E731
            backend, resource, timeout=args.timeout
        )

        # ---- 1. one session: sync I/O on one thread, async ops on others ---
        with open_session() as inst:
            lib, sess = inst.visalib, inst.session
            idn = inst.query("*IDN?").strip()
            visa.drain_errors(inst)

            stop = threading.Event()
            problems: list[str] = []
            counts = {"sync": 0, "stb": 0, "lock": 0, "ren": 0, "lock_timeout": 0}
            counts_lock = threading.Lock()

            def bump(key: str) -> None:
                with counts_lock:
                    counts[key] += 1

            def sync_worker() -> None:
                while not stop.is_set():
                    try:
                        got = inst.query("*IDN?").strip()
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"query raised {type(exc).__name__}: {exc}")
                        continue
                    if got != idn:
                        problems.append(f"query returned {got!r}")
                    bump("sync")

            def stb_worker() -> None:
                while not stop.is_set():
                    try:
                        stb = inst.read_stb()
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"read_stb raised {type(exc).__name__}: {exc}")
                        continue
                    if not isinstance(stb, int) or not 0 <= stb <= 0xFF:
                        problems.append(f"implausible status byte {stb!r}")
                    bump("stb")

            def lock_worker() -> None:
                # An exclusive lock cannot be granted while the sync channel
                # has work in flight, and this test keeps it saturated on
                # purpose, so a VI_ERROR_TMO here is the specified behaviour
                # rather than a fault.
                while not stop.is_set():
                    _, st = visa.call(
                        lib.lock, sess, constants.Lock.exclusive, 2000, None
                    )
                    if st == constants.StatusCode.error_timeout:
                        bump("lock_timeout")
                        continue
                    if st != constants.StatusCode.success:
                        problems.append(f"lock failed with {st!r}")
                        continue
                    st = visa.status(lib.unlock, sess)
                    if st == constants.StatusCode.error_timeout:
                        bump("lock_timeout")
                        continue
                    if st != constants.StatusCode.success:
                        problems.append(f"unlock failed with {st!r}")
                    bump("lock")

            mode = ren_mode(args.protocol)

            def ren_worker() -> None:
                while not stop.is_set():
                    st = visa.status(lib.gpib_control_ren, sess, mode)
                    if st != constants.StatusCode.success:
                        problems.append(f"REN failed with {st!r}")
                    bump("ren")

            workers = [threading.Thread(target=sync_worker, daemon=True)]
            for index in range(args.threads):
                target = (stb_worker, lock_worker, ren_worker)[index % 3]
                workers.append(threading.Thread(target=target, daemon=True))

            duration = max(2.0, args.iterations / 100.0)
            for worker in workers:
                worker.start()
            time.sleep(duration)
            stop.set()
            for worker in workers:
                worker.join(timeout=10.0)

            alive = [w.name for w in workers if w.is_alive()]
            stats.check(
                not alive,
                "all worker threads stopped cleanly",
                detail=f"{len(workers)} workers, still running: {alive or None}",
            )
            stats.check(
                not problems,
                "concurrent sessions ran without interfering",
                detail=f"{problems[:3]}",
            )
            stats.note(
                f"in {duration:.0f}s: {counts['sync']} queries, {counts['stb']} "
                f"status queries, {counts['lock']} lock cycles, "
                f"{counts['ren']} REN ops"
            )
            if counts["lock_timeout"]:
                stats.note(
                    f"{counts['lock_timeout']} lock attempts timed out: expected, "
                    f"an exclusive lock waits for the saturated sync channel to "
                    f"go idle"
                )
            stats.check(
                counts["sync"] > 0 and counts["stb"] > 0,
                "both channels were actually driven",
                detail=f"{counts['sync']} queries, {counts['stb']} status queries",
            )
            final = inst.query("*IDN?").strip()
            stats.check(
                final == idn,
                "the session is healthy after the concurrent load",
                detail=f"got {final!r}",
            )
            visa.check_errors(inst, stats, "after single-session concurrency")

        # Let the server reclaim the closed session before opening a batch of
        # new ones, so phase 2 measures itself and not phase 1's aftermath.
        time.sleep(1.0)

        # ---- 2. many sessions in parallel ----------------------------------
        session_problems: list[str] = []

        # Over VXI-11 each query has to be held under a lock. The transport
        # carries device_write and device_read as separate RPCs and does not
        # fuse them, so two unlocked sessions querying one instrument
        # interleave: both writes land, the first read drains the whole output
        # queue -- returning two concatenated replies -- and the second finds
        # nothing and waits out its timeout. That is what a real bus does with
        # two controllers and no lock, so the guarantee being checked here is
        # the locked one. HiSLIP needs no lock because its server holds the bus
        # across the write and read of a single query.
        needs_lock = args.protocol == "vxi11"

        def session_worker(index: int) -> None:
            try:
                with open_session() as local:
                    for _ in range(max(5, args.iterations // 20)):
                        if needs_lock:
                            local.lock_excl(10000)
                        try:
                            got = local.query("*IDN?").strip()
                            # Inside the lock as well: VXI-11 refuses
                            # device_readstb while another link holds the
                            # device (B.5.2 error 11), so a status poll left
                            # outside the lock fails with VI_ERROR_RSRC_LOCKED
                            # the moment a sibling takes it -- correctly, and
                            # for a reason that has nothing to do with the
                            # thing being tested.
                            local.read_stb()
                        finally:
                            if needs_lock:
                                local.unlock()
                        if got != idn:
                            session_problems.append(f"session {index} got {got!r}")
                            return
            except Exception as exc:  # noqa: BLE001
                session_problems.append(
                    f"session {index}: {type(exc).__name__}: {exc}"
                )

        threads = [
            threading.Thread(target=session_worker, args=(i,))
            for i in range(args.sessions)
        ]
        t0 = time.time()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120.0)
        stats.check(
            not session_problems,
            "parallel sessions to one instrument do not interfere"
            + (" (locked, as VXI-11 requires)" if needs_lock else ""),
            rule="VPP-4.3 3.1.3",
            detail=f"{args.sessions} sessions; {session_problems[:3]}",
        )
        stats.note(f"{args.sessions} parallel sessions in {time.time() - t0:.1f}s")

        # ---- 3. open/close churn: threads and sockets must be reclaimed -----
        base_threads = threading.active_count()
        base_fds = visa.open_fd_count()
        cycles = max(10, args.iterations // 10)
        # Guarded: reopening the same resource in a tight loop is exactly where
        # a transient resolution failure shows up -- VI_ERROR_RSRC_NFOUND on
        # the seventh cycle, seen on Windows -- and an unguarded raise here
        # takes the two leak checks below with it, so a churn that broke reads
        # as a leak check that was never written.
        completed = 0
        with stats.attempt(
            "repeated open/close cycles all reopen the resource",
            detail=f"{cycles} cycles requested",
        ) as churned:
            for _ in range(cycles):
                with open_session() as churn:
                    churn.query("*IDN?")
                completed += 1
                churned.detail = f"{completed}/{cycles} cycles"
        if not churned:
            stats.note(
                f"the churn stopped after {completed}/{cycles} cycles, so the "
                f"leak deltas below cover only those"
            )
        time.sleep(1.0)  # give any lingering threads a chance to exit

        leaked_threads = threading.active_count() - base_threads
        stats.check(
            leaked_threads <= 1,
            "repeated open/close cycles leak no threads",
            detail=f"{completed} cycles, delta {leaked_threads}",
        )
        # No fd directory to count on Windows. Saying so beats subtracting two
        # sentinels and reporting a delta of zero as a pass.
        if base_fds < 0:
            stats.skip(
                "repeated open/close cycles leak no file descriptors",
                "this platform exposes no open-descriptor count",
            )
        else:
            leaked_fds = visa.open_fd_count() - base_fds
            stats.check(
                leaked_fds <= 2,
                "repeated open/close cycles leak no file descriptors",
                detail=f"{completed} cycles, delta {leaked_fds}",
            )
        stats.note(
            f"after {completed} cycles: {threading.active_count()} threads, "
            f"{visa.open_fd_count()} fds"
        )

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
