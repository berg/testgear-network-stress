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

import contextlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402

from testgear import harness, script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}

#: Phase 2 gives each session thread two minutes to finish, so the check that
#: waits on them has to outlast that.
PARALLEL_WATCHDOG = 300.0


def add_arguments(parser) -> None:
    parser.add_argument("--threads", type=int, default=4, help="async worker threads")
    parser.add_argument("--sessions", type=int, default=6, help="parallel sessions")


def ren_mode(protocol: str) -> constants.RENLineOperation:
    """A remote/local operation the transport actually carries.

    VXI-11 has no RPC for driving REN without addressing (B.6.13), so the
    unaddressed assert is refused there and would report a fault on every
    iteration of a worker whose job is to make unrelated traffic.
    """
    if protocol == "vxi11":
        return constants.RENLineOperation.asrt_address
    return constants.RENLineOperation.asrt


def open_session():
    return visa.session(CTX["backend"], CTX["resource"], timeout=CTX["timeout"])


@contextlib.contextmanager
def SETUP(ctx):
    """No long-lived session here: each phase opens its own.

    Phase 1 has to close its session before phase 3 can measure what closing a
    session reclaims, and phase 2 is about sessions opened in parallel. All
    that is shared is the identification string the phases compare against.
    """
    with open_session() as session:
        STATE["idn"] = session.query("*IDN?").strip()
        visa.drain_errors(session)
    yield


# ---- 1. one session: sync I/O on one thread, async ops on others -----------
def concurrent_load() -> dict:
    """Saturate one session from several threads, once.

    Four checks read this one run. Opening the session inside it, and closing
    it on the way out, is what lets phase 3 measure a clean baseline: a
    session still open when the leak counters are taken reads as a leak.
    """
    if "load" in STATE:
        return STATE["load"]

    args, stats = CTX["args"], CTX["stats"]
    idn = STATE["idn"]
    result: dict = {"problems": [], "counts": {}, "alive": [], "final": None}
    STATE["load"] = result

    with open_session() as inst:
        lib, sess = inst.visalib, inst.session
        stop = threading.Event()
        problems: list[str] = result["problems"]
        counts = {"sync": 0, "stb": 0, "lock": 0, "ren": 0, "lock_timeout": 0}
        result["counts"] = counts
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
            # An exclusive lock cannot be granted while the sync channel has
            # work in flight, and this test keeps it saturated on purpose, so
            # a VI_ERROR_TMO here is the specified behaviour rather than a
            # fault.
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
        result["duration"] = duration
        result["workers"] = len(workers)
        for worker in workers:
            worker.start()
        time.sleep(duration)
        stop.set()
        for worker in workers:
            worker.join(timeout=10.0)
        result["alive"] = [w.name for w in workers if w.is_alive()]

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
        result["final"] = inst.query("*IDN?").strip()
        visa.check_errors(inst, stats, "after single-session concurrency")
    return result


@check("all worker threads stopped cleanly")
def check_workers_stopped():
    result = concurrent_load()
    alive = result["alive"]
    detail = f"{result['workers']} workers, still running: {alive or None}"
    assert not alive, detail
    return detail


@check("concurrent sessions ran without interfering")
def check_no_interference():
    result = concurrent_load()
    problems = result["problems"]
    detail = f"{result['workers']} workers over {result['duration']:.0f}s, " + (
        f"{len(problems)} problems: {problems[:3]}" if problems else "no problems"
    )
    assert not problems, detail
    return detail


@check("both channels were actually driven")
def check_both_channels_driven():
    """A run where the async workers never got a turn proves nothing about
    demultiplexing, and would otherwise pass every check above."""
    counts = concurrent_load()["counts"]
    detail = f"{counts['sync']} queries, {counts['stb']} status queries"
    assert counts["sync"] > 0 and counts["stb"] > 0, detail
    return detail


@check("the session is healthy after the concurrent load")
def check_healthy_after_load():
    final = concurrent_load()["final"]
    assert final == STATE["idn"], f"got {final!r}"
    return f"got {final!r}"


# ---- 2. many sessions in parallel ------------------------------------------
def _parallel_sessions():
    """Query one instrument from several sessions at once.

    Over VXI-11 each query has to be held under a lock. The transport carries
    device_write and device_read as separate RPCs and does not fuse them, so
    two unlocked sessions querying one instrument interleave: both writes
    land, the first read drains the whole output queue -- returning two
    concatenated replies -- and the second finds nothing and waits out its
    timeout. That is what a real bus does with two controllers and no lock, so
    the guarantee being checked here is the locked one. HiSLIP needs no lock
    because its server holds the bus across the write and read of a single
    query.
    """
    args, stats = CTX["args"], CTX["stats"]
    # Let the server reclaim phase 1's session before opening a batch of new
    # ones, so this phase measures itself and not phase 1's aftermath.
    time.sleep(1.0)
    session_problems: list[str] = []
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
                        # device_readstb while another link holds the device
                        # (B.5.2 error 11), so a status poll left outside the
                        # lock fails with VI_ERROR_RSRC_LOCKED the moment a
                        # sibling takes it -- correctly, and for a reason that
                        # has nothing to do with the thing being tested.
                        local.read_stb()
                    finally:
                        if needs_lock:
                            local.unlock()
                    if got != STATE["idn"]:
                        session_problems.append(f"session {index} got {got!r}")
                        return
        except Exception as exc:  # noqa: BLE001
            session_problems.append(f"session {index}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=session_worker, args=(i,))
        for i in range(args.sessions)
    ]
    t0 = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120.0)
    stats.note(f"{args.sessions} parallel sessions in {time.time() - t0:.1f}s")
    detail = f"{args.sessions} sessions; {session_problems[:3]}"
    assert not session_problems, detail
    return detail


def _register_parallel_check() -> None:
    """One name per transport, because the claim differs.

    Over VXI-11 the guarantee only holds under a lock, and saying so in the
    name is the difference between "these sessions do not interfere" and
    "these sessions do not interfere, given the locking the transport
    requires". Registering both and letting the protocol filter choose keeps
    the name fixed at import, which is what the matrix joins on.
    """
    add = harness.registrar(globals())
    add(
        _parallel_sessions,
        "parallel sessions to one instrument do not interfere",
        rule="VPP-4.3 3.1.3",
        protocols=("hislip",),
        watchdog=PARALLEL_WATCHDOG,
    )
    add(
        _parallel_sessions,
        "parallel sessions to one instrument do not interfere "
        "(locked, as VXI-11 requires)",
        rule="VPP-4.3 3.1.3",
        protocols=("vxi11",),
        watchdog=PARALLEL_WATCHDOG,
    )


_register_parallel_check()


# ---- 3. open/close churn: threads and sockets must be reclaimed ------------
def churn() -> dict:
    """Open and close the resource repeatedly, counting what is left behind.

    Guarded, because reopening the same resource in a tight loop is exactly
    where a transient resolution failure shows up -- VI_ERROR_RSRC_NFOUND on
    the seventh cycle, seen on Windows -- and an unguarded raise here takes
    the two leak checks below with it, so a churn that broke would read as a
    leak check that was never written.
    """
    if "churn" in STATE:
        return STATE["churn"]

    args = CTX["args"]
    result: dict = {
        "base_threads": threading.active_count(),
        "base_fds": visa.open_fd_count(),
        "cycles": max(10, args.iterations // 10),
        "completed": 0,
        "error": None,
    }
    STATE["churn"] = result
    try:
        for _ in range(result["cycles"]):
            with open_session() as session:
                session.query("*IDN?")
            result["completed"] += 1
    except Exception as exc:  # noqa: BLE001
        result["error"] = exc
        CTX["stats"].note(
            f"the churn stopped after {result['completed']}/{result['cycles']} "
            f"cycles, so the leak deltas below cover only those"
        )
    time.sleep(1.0)  # give any lingering threads a chance to exit
    result["threads"] = threading.active_count()
    result["fds"] = visa.open_fd_count()
    CTX["stats"].note(
        f"after {result['completed']} cycles: {result['threads']} threads, "
        f"{result['fds']} fds"
    )
    return result


@check("repeated open/close cycles all reopen the resource")
def check_churn_completes():
    result = churn()
    if result["error"] is not None:
        raise AssertionError(
            f"{result['completed']}/{result['cycles']} cycles: "
            f"{type(result['error']).__name__}: {result['error']}"
        )
    return f"{result['completed']}/{result['cycles']} cycles"


@check("repeated open/close cycles leak no threads")
def check_no_thread_leak():
    result = churn()
    leaked = result["threads"] - result["base_threads"]
    detail = f"{result['completed']} cycles, delta {leaked}"
    assert leaked <= 1, detail
    return detail


@check("repeated open/close cycles leak no file descriptors")
def check_no_fd_leak():
    """No fd directory to count on Windows. Saying so beats subtracting two
    sentinels and reporting a delta of zero as a pass."""
    result = churn()
    if result["base_fds"] < 0:
        raise Skip("this platform exposes no open-descriptor count")
    leaked = result["fds"] - result["base_fds"]
    detail = f"{result['completed']} cycles, delta {leaked}"
    assert leaked <= 2, detail
    return detail


if __name__ == "__main__":
    script.run()
