#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write what a leg did, including when it did nothing.

Uploaded from every leg with `if: always()`, so the aggregation step learns
about a job that died from the job itself rather than by noticing an absence.
That distinction is the whole point: a column missing from the matrix reads as
agreement between the implementations that remain, and a run that crashed
before it produced anything is the case most likely to go unnoticed.

    tools/leg_status.py --leg "$LEG_JSON" --dir out --out out/leg.json \\
        --status ok

Status is inferred from what is in `--dir` unless `--status` overrides it,
because the common case -- reports present, some checks failed -- is exactly
the case a shell `if:` would get wrong.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import aggregate  # noqa: E402

#: Exit codes that mean the suite is broken rather than the backend is. See
#: testgear.harness.main: 2 is an unexpected exception, 5 is this suite calling
#: VISA wrongly. Neither is ever a finding about an implementation.
OUR_BUG = (2, 5)

#: The target went away. Not a library regression, and deliberately not fatal:
#: a flaky bench reported as a regression wastes the next person's afternoon,
#: and a shared cloud runner is a flakier bench than a desk.
LOST_TARGET = 3


def classify(reports: list[Path], exit_codes: dict) -> tuple[str, str, bool]:
    """(status, reason, flaky) for one leg."""
    # exit_codes is {file: {column label: {script: rc}}} -- one file per
    # transport, one column per backend the leg compared.
    flat: dict[str, int | None] = {}
    for per_file in exit_codes.values():
        for per_column in (per_file or {}).values():
            if isinstance(per_column, dict):
                flat.update(per_column)

    ours = sorted(s for s, rc in flat.items() if rc in OUR_BUG)
    if ours:
        return (
            "errored",
            f"the suite itself failed in {', '.join(ours)} "
            f"(exit {flat[ours[0]]}); this is our bug, not a finding",
            False,
        )
    flaky = any(rc == LOST_TARGET for rc in flat.values())
    if not reports:
        return "not-run", "the leg produced no report", flaky
    return "ok", "", flaky


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--leg", required=True, help="the leg descriptor, as JSON")
    parser.add_argument("--dir", required=True, help="where the leg wrote its output")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--status",
        choices=aggregate.STATUSES,
        help="override the inferred status, for a leg that knows it failed "
        "before it ran anything -- a vendor library that would not initialise",
    )
    parser.add_argument("--reason", default="")
    parser.add_argument("--duration", type=float, default=0.0)
    args = parser.parse_args()

    leg = json.loads(args.leg)
    root = Path(args.dir)
    reports = aggregate.column_files(root) if root.is_dir() else []
    exit_codes: dict = {}
    for path in sorted(root.glob("*.rc.json")) if root.is_dir() else []:
        exit_codes[path.name] = json.loads(path.read_text(encoding="utf-8"))

    status, reason, flaky = classify(reports, exit_codes)
    if args.status:
        status, reason = args.status, args.reason or reason
    elif args.reason:
        reason = args.reason

    leg.update(
        status=status,
        reason=reason,
        flaky=flaky,
        exit_codes=exit_codes,
        duration=round(args.duration, 1),
        host=f"{platform.system()} {platform.machine()}",
        reports=[p.name for p in reports],
    )
    Path(args.out).write_text(json.dumps(leg, indent=2) + "\n", encoding="utf-8")
    print(f"{leg.get('id', '?')}: {status}" + (f" -- {reason}" if reason else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
