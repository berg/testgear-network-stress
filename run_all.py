#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the whole suite, on any platform.

The same sweep as run_all.sh, in Python so it works where bash does not --
which in practice means Windows, where Keysight and Tektronix ship the only
VISA implementations this suite cannot otherwise reach.

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

# Per transport, in order. `None` means both; otherwise the one transport whose
# wire protocol the script inspects directly.
SCRIPTS: list[tuple[str, list[str], str | None]] = [
    ("01_smoke.py", [], None),
    ("02_io.py", ["-n", "{iter}"], None),
    ("03_srq.py", ["-n", "30"], None),
    ("04_concurrency.py", ["-n", "{iter}"], None),
    ("05_lock.py", ["-n", "{iter}"], None),
    ("06_terminate.py", ["-n", "15"], None),
    ("07_clear.py", ["-n", "40"], None),
    ("09_remote_local.py", [], None),
    ("10_lock_semantics.py", [], None),
    ("12_session_lifecycle.py", [], None),
    ("13_events.py", [], None),
    ("15_required_attributes.py", [], None),
    ("16_operations.py", [], None),
    ("17_resource_names.py", [], None),
    ("conformance.py", [], None),
    ("08_soak.py", ["--duration", "{soak}", "--srq-thread"], None),
    ("vxi11_conformance.py", [], "vxi11"),
    ("14_vxi11_flags.py", [], "vxi11"),
    ("11_hislip_messages.py", [], "hislip"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocol", choices=("hislip", "vxi11"))
    parser.add_argument("--reports", help="directory for per-script JSON")
    parser.add_argument("--soak", type=int, default=60)
    parser.add_argument("--iterations", type=int, default=300)
    args, passthrough = parser.parse_known_args()

    protocols = [args.protocol] if args.protocol else ["hislip", "vxi11"]
    reports = pathlib.Path(args.reports) if args.reports else None
    if reports:
        reports.mkdir(parents=True, exist_ok=True)

    failed = ran = skipped = 0
    for proto in protocols:
        for name, extra, only in SCRIPTS:
            if only and only != proto:
                continue
            ran += 1
            cmd = [sys.executable, str(HERE / "checks" / name), "--protocol", proto]
            cmd += [
                a.format(iter=args.iterations, soak=args.soak) for a in extra
            ]
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
