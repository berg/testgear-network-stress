#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the whole suite, on any platform.

The sweep itself. `run_all.sh` is a shim over this one, kept for the habit of
typing it and for the REPORTS/SOAK/ITER environment variables; the sweep is
here because it has to work where bash does not -- which in practice means
Windows, where Keysight and Tektronix ship the only VISA implementations this
suite cannot otherwise reach.

Which scripts run, in what order, with what arguments, comes from
`testgear.suite` -- shared with `compare.py`, so the matrix and the sweep can
never disagree about what the suite is.

    python run_all.py                                 # everything, both transports
    python run_all.py --protocol hislip               # one transport
    python run_all.py --backend keysight              # a different VISA
    python run_all.py --reports reports-keysight      # JSON for the matrix

Anything this does not recognise is passed through to each check script, so
every option the scripts accept works here too.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from testgear import suite  # noqa: E402

#: Seconds a check script may take before the runner kills it. Generous,
#: because the number that matters is not "how long should this take" but "how
#: long before a human staring at a silent terminal concludes it is wedged" --
#: and against real hardware over a network, a script doing 80 sequential
#: open/close cycles is legitimately slow.
DEFAULT_SCRIPT_TIMEOUT = 300.0

#: What the output of a script that could not reach the target looks like. An
#: instrument whose server has stopped accepting connections fails every check
#: in every later script with this, and the summary then reads as dozens of
#: lock and event findings rather than as the one event it is.
_TARGET_GONE = re.compile(
    r"VI_ERROR_RSRC_NFOUND|VI_ERROR_CONN_LOST|error_resource_nfound"
)


def _terminate(proc: subprocess.Popen) -> None:
    """End a script that outstayed its budget, politely and then not.

    A wedged script is usually blocked inside the VISA library rather than in
    Python, so SIGTERM reaches a process that is in no position to handle it.
    Kill the whole process group: the checks spawn threads, and on POSIX a
    vendor library is entitled to have spawned helpers of its own.
    """
    for sig, fallback in ((signal.SIGTERM, proc.terminate),
                          (signal.SIGKILL, proc.kill)):
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), sig)
            else:
                fallback()
        except (ProcessLookupError, PermissionError, OSError):
            return  # already gone
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_script(cmd: list[str], timeout: float) -> tuple[int | None, str]:
    """Run one check script, echoing its output as it arrives.

    Returns `(returncode, output)`, with `None` for a script that had to be
    killed.

    This used to be `subprocess.run(capture_output=True)`, which has two
    failure modes that compound into one bad afternoon. Captured output is
    printed only after the child exits, so a child that never exits prints
    nothing at all -- and the last thing on the terminal is the header line
    this runner printed before starting it, which reads as "it hung on the
    first check" no matter where it actually stopped. Then there was no
    timeout, so the sweep waited for it forever.

    `PYTHONUNBUFFERED` matters as much as the streaming does: the child's own
    stdout is a pipe here, so Python block-buffers it, and 40 lines of results
    fit inside the buffer without ever being flushed. Streaming a buffer that
    is never written is still nothing.
    """
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=os.name == "posix",
    )
    captured: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)
            print(line, end="", flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
        killed = False
    except subprocess.TimeoutExpired:
        _terminate(proc)
        killed = True
    # Let the pump drain whatever was already in the pipe. It is a daemon, so
    # a reader still blocked after this does not hold up the sweep.
    reader.join(5)
    return (None if killed else proc.returncode), "".join(captured)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocol", choices=("hislip", "vxi11"))
    parser.add_argument("--reports", help="directory for per-script JSON")
    parser.add_argument("--soak", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument(
        "--sessions",
        type=int,
        metavar="N",
        help="how many sessions 04_concurrency.py opens to the instrument at "
        "once. Left unset it uses that script's own default (6). Some "
        "instruments cap concurrent connections well below that and stop "
        "accepting any for the rest of the run once you pass it",
    )
    parser.add_argument(
        "--script-timeout",
        type=float,
        default=DEFAULT_SCRIPT_TIMEOUT,
        metavar="SECONDS",
        help="kill a check script that has run this long (default "
        f"{DEFAULT_SCRIPT_TIMEOUT:.0f}s). The soak gets its --soak duration on "
        "top of this.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every script even after the target stops answering. The "
        "default stops, because every later script then fails every check "
        "for the same one reason and the summary reads as dozens of findings",
    )
    args, passthrough = parser.parse_known_args()

    protocols = (args.protocol,) if args.protocol else ("hislip", "vxi11")
    reports = pathlib.Path(args.reports) if args.reports else None
    if reports:
        reports.mkdir(parents=True, exist_ok=True)

    failed = ran = skipped = timedout = 0
    # Consecutive scripts that could not reach the target. One is a check
    # leaving the instrument busy; two in a row is the instrument itself
    # gone, and nothing after it will mean anything.
    unreachable = 0
    abandoned: list[str] = []
    for proto in protocols:
        for script in suite.for_protocol(proto):
            ran += 1
            name = script.name
            cmd = [sys.executable, str(HERE / "checks" / name), "--protocol", proto]
            cmd += script.argv(
                iterations=args.iterations,
                soak=args.soak,
                sessions=args.sessions,
            )
            if reports:
                cmd += ["--report", str(reports / f"{name[:-3]}-{proto}.json")]
            cmd += passthrough

            budget = script.budget(
                default=args.script_timeout,
                iterations=args.iterations,
                soak=args.soak,
            )
            print(f"\n{'=' * 63}\n=== {name} [{proto}]", flush=True)
            code, out = run_script(cmd, budget)
            if code is None:
                timedout += 1
                failed += 1
                abandoned.append(f"{name} [{proto}]")
                print(
                    f">>> {name} was still running after {budget:.0f}s and was "
                    f"killed. Everything above is what it managed to report; "
                    f"it wrote no JSON report, so the matrix will show this "
                    f"column as not having run."
                )
                print(
                    ">>> raise --script-timeout if this script is legitimately "
                    "slow here, or look at what it was doing when it stopped "
                    "printing."
                )
            else:
                # Exit 3 is the target going away rather than a check failing. A
                # flaky bench reported as a library regression wastes the next
                # person's afternoon.
                if code == 3:
                    print(">>> lost the connection to the target")
                if code != 0:
                    failed += 1
            skipped += len(re.findall(r"^\s*SKIP ", out, re.M))

            # A script that timed out tells us nothing about whether the
            # target is still there; it may have been killed mid-open.
            if code is None:
                continue
            if code == 3 or (code != 0 and _TARGET_GONE.search(out)):
                unreachable += 1
            else:
                unreachable = 0
            if unreachable >= 2 and not args.keep_going:
                print(
                    f"\n{'=' * 63}\n"
                    f">>> the target stopped accepting connections and the last "
                    f"{unreachable} scripts failed every check for that one "
                    f"reason.\n"
                    f">>> stopping here. Carrying on would add hundreds of "
                    f"failures that all say the same thing, under the names of "
                    f"checks that never ran.\n"
                    f">>> the instrument likely needs a moment or a power cycle "
                    f"-- a server with a link or session cap does not always "
                    f"release them when the client goes away. --keep-going runs "
                    f"the rest anyway."
                )
                print(f"\n{'=' * 63}\n{ran} scripts run, sweep stopped early")
                if timedout:
                    print(f"{timedout} script(s) were killed on a timeout: "
                          + ", ".join(abandoned))
                return failed or 1

    print(f"\n{'=' * 63}\n{ran} scripts run")
    print("all scripts passed" if not failed else f"{failed} script(s) reported failures")
    if timedout:
        print(
            f"{timedout} script(s) never finished and were killed: "
            + ", ".join(abandoned)
        )
    if skipped:
        print(f"{skipped} check(s) were SKIPPED and are not passes -- see the SKIP")
        print("lines above for why each one could not run.")
    if reports:
        print(f"JSON reports written to {reports}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
