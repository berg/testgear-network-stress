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
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from testgear import suite  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocol", choices=("hislip", "vxi11"))
    parser.add_argument("--reports", help="directory for per-script JSON")
    parser.add_argument("--soak", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=300)
    args, passthrough = parser.parse_known_args()

    protocols = (args.protocol,) if args.protocol else ("hislip", "vxi11")
    reports = pathlib.Path(args.reports) if args.reports else None
    if reports:
        reports.mkdir(parents=True, exist_ok=True)

    failed = ran = skipped = 0
    for proto in protocols:
        for script in suite.for_protocol(proto):
            ran += 1
            name = script.name
            cmd = [sys.executable, str(HERE / "checks" / name), "--protocol", proto]
            cmd += script.argv(iterations=args.iterations, soak=args.soak)
            if reports:
                cmd += ["--report", str(reports / f"{name[:-3]}-{proto}.json")]
            cmd += passthrough

            print(f"\n{'=' * 63}\n=== {name} [{proto}]", flush=True)
            done = subprocess.run(cmd, capture_output=True, text=True)
            out = done.stdout + done.stderr
            print(out, end="", flush=True)
            # Exit 3 is the target going away rather than a check failing. A
            # flaky bench reported as a library regression wastes the next
            # person's afternoon.
            if done.returncode == 3:
                print(">>> lost the connection to the target")
            if done.returncode != 0:
                failed += 1
            skipped += len(re.findall(r"^\s*SKIP ", out, re.M))

    print(f"\n{'=' * 63}\n{ran} scripts run")
    print("all scripts passed" if not failed else f"{failed} script(s) reported failures")
    if skipped:
        print(f"{skipped} check(s) were SKIPPED and are not passes -- see the SKIP")
        print("lines above for why each one could not run.")
    if reports:
        print(f"JSON reports written to {reports}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
