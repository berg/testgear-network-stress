#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The verdict: did this run find a regression, or just do its job?

    tools/ci_status.py --columns columns/
    tools/ci_status.py --columns columns/ --write-baseline

Neither runner's exit status can answer this. compare.py always exits 0 on
purpose -- disagreement is the finding, not an error -- and run_all exits with
a count of failed scripts, which conflates a check failing with the bench going
away. So the verdict is computed here, from the columns and the exit codes.

What is red:

  exit 2, 5   the suite is broken. 2 is an unexpected exception, 5 is this
              suite calling VISA wrongly. Never a finding about a backend.
  regression  a check the baseline says passed now fails, or now skips.

What is not:

  exit 1      a check failed. That is the product. pyvisa-py fails about
              twenty checks today and docs/findings.md is the list.
  exit 3      the target went away. Flagged, never fatal: a flaky bench
              reported as a library regression wastes the next person's
              afternoon, and a shared cloud runner is a flakier bench than a
              desk.
  exit 4      a backend is not installed. Reported as an unavailable column.

A new SKIP counts as a regression alongside a new FAIL, deliberately. In the
suite this one grew out of, the large-reply checks stayed skipped through an
entire development cycle without anyone noticing, because at a glance a
skipped check reads like a passing one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_matrix import load  # noqa: E402

from testgear import aggregate  # noqa: E402

OUR_BUG = (2, 5)
LOST_TARGET = 3
REGRESSIONS = {("PASS", "FAIL"), ("PASS", "SKIP")}


def annotate(level: str, message: str) -> None:
    """A GitHub Actions annotation, and a plain line for a human terminal."""
    print(f"::{level}::{message}")


def outcomes_for(
    columns: list[dict], subject: str, leg: str
) -> tuple[dict[str, str], str]:
    """One column's outcome per check key, and which column that was.

    Deliberately one column, not every column of the subject merged. pyvisa-py
    on Windows skips the descriptor-leak check for want of /dev/fd, and folding
    that into the baseline would make the recorded outcome depend on which legs
    happened to run -- so a Windows runner going missing would read as a pile
    of improvements. The gate wants one reference platform; the cross-platform
    comparison is the page's job.
    """
    matrix = aggregate.build(columns)
    wanted = [
        i for i in matrix.subject_columns(subject) if columns[i].get("id") == leg
    ] or matrix.subject_columns(subject)
    if not wanted:
        return {}, ""
    at = wanted[0]
    return (
        {r.key: r.outcomes[at] for r in matrix.rows if r.outcomes[at] != "-"},
        columns[at].get("id") or aggregate.label(columns[at]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--columns", required=True)
    parser.add_argument(
        "--baseline", default=str(Path(__file__).parent.parent / "docs" / "ci-baseline.json")
    )
    parser.add_argument("--subject", default="py")
    parser.add_argument(
        "--subject-leg",
        default="linux-py",
        help="which leg is the reference platform for the gate. One column, "
        "not all of the subject's, so the recorded outcome does not depend on "
        "which legs happened to run",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this run as the baseline instead of judging against it",
    )
    parser.add_argument(
        "--gate-subject",
        action="store_true",
        default=True,
        help="fail on a regression in the subject column (default)",
    )
    parser.add_argument(
        "--no-gate-subject",
        dest="gate_subject",
        action="store_false",
        help="report regressions without failing -- for a full run, whose "
        "output is the page rather than a verdict",
    )
    args = parser.parse_args()

    root = Path(args.columns)
    protocols = [d.name for d in sorted(root.iterdir()) if d.is_dir()]
    current: dict[str, dict[str, str]] = {}
    seen_index: dict[str, list] = {}
    verdict = 0
    flaky_legs: set[str] = set()

    for protocol in protocols:
        columns = load(root / protocol)
        if not columns:
            continue
        outcomes, from_leg = outcomes_for(columns, args.subject, args.subject_leg)
        if not outcomes:
            annotate(
                "warning",
                f"[{protocol}] no {args.subject} column ran, so there is "
                f"nothing to judge against the baseline",
            )
        else:
            current[protocol] = outcomes
            if from_leg != args.subject_leg:
                annotate(
                    "warning",
                    f"[{protocol}] judged against {from_leg}, not "
                    f"{args.subject_leg}, which did not run",
                )

        seen_index[protocol] = json.loads(
            (root / protocol / "index.json").read_text()
        )["columns"]

    # Exit codes are recorded per leg, covering every transport, and the same
    # leg appears in each protocol's index. Walk them once, keyed by leg and
    # script, or every crash is reported once per transport directory and
    # labelled with whichever one happened to be iterating.
    crashes: dict[tuple[str, str], int] = {}
    lost: dict[tuple[str, str], int] = {}
    for index in seen_index.values():
        for entry in index:
            for filename, per_file in (entry.get("exit_codes") or {}).items():
                # The transport is in the filename, not in the keys: a script
                # runs once per transport and the same name appears twice.
                transport = filename.replace(".rc.json", "")
                for column_codes in (per_file or {}).values():
                    if not isinstance(column_codes, dict):
                        continue
                    for script, code in column_codes.items():
                        where = f"{script} [{transport}]"
                        if code in OUR_BUG:
                            crashes[(entry["id"], where)] = code
                        elif code == LOST_TARGET:
                            lost[(entry["id"], where)] = code
            if entry.get("flaky"):
                flaky_legs.add(entry["id"])

    for (leg, script), code in sorted(crashes.items()):
        verdict = 1
        annotate(
            "error",
            f"{leg}: {script} exited {code}. The suite itself is broken here, "
            f"which is never a finding about a backend -- and the checks that "
            f"script would have run are absent from the column, not passing.",
        )
    for (leg, script), _ in sorted(lost.items()):
        annotate(
            "warning",
            f"{leg}: {script} lost the target (exit 3). Not counted as a "
            f"failure.",
        )

    reported: set[str] = set()
    for protocol, index in seen_index.items():
        for entry in index:
            if entry.get("status") == "errored":
                verdict = 1
                annotate("error", f"{entry['id']}: {entry.get('reason', '')}")
            elif entry.get("status") not in ("ok", None):
                key = f"{entry['id']}/{protocol}"
                if key not in reported:
                    reported.add(key)
                    annotate(
                        "warning",
                        f"{entry['id']} produced no {protocol} column: "
                        f"{entry.get('reason', '')}",
                    )

    if args.write_baseline:
        Path(args.baseline).write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        total = sum(len(v) for v in current.values())
        print(f"baseline written: {total} checks across {len(current)} transports")
        return 0

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        annotate(
            "warning",
            f"no baseline at {baseline_path}; nothing to compare against. "
            f"Write one with --write-baseline from a run you trust.",
        )
        return verdict

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    regressions, improvements, unknown = [], [], 0
    for protocol, outcomes in current.items():
        was = baseline.get(protocol, {})
        for key, now in outcomes.items():
            before = was.get(key)
            if before is None:
                unknown += 1
            elif (before, now) in REGRESSIONS:
                regressions.append((protocol, key, before, now))
            elif before == "FAIL" and now == "PASS":
                improvements.append((protocol, key))

    for protocol, key, before, now in regressions:
        annotate("error", f"[{protocol}] {before} -> {now}: {key}")
    if regressions and args.gate_subject:
        verdict = 1

    if improvements:
        annotate(
            "notice",
            f"{len(improvements)} check(s) now pass that the baseline records "
            f"as failing. The baseline is stale -- refresh it with "
            f"tools/ci_status.py --write-baseline.",
        )
    if unknown:
        print(f"{unknown} check(s) the baseline has never seen; not judged.")
    if flaky_legs:
        annotate(
            "warning",
            f"the target went away at least once in: {', '.join(sorted(flaky_legs))}",
        )

    print(
        f"\n{len(regressions)} regression(s), {len(improvements)} improvement(s). "
        f"verdict: {'FAIL' if verdict else 'ok'}"
    )
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
