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


def classify(reports: list[Path], exit_codes: dict) -> tuple[str, str, bool, list]:
    """(status, reason, flaky, errors) for one leg.

    `status` is about whether this leg produced columns at all. `errors` is a
    separate question -- which scripts crashed -- and the two must not be
    collapsed. A leg where one script exits 2 has still produced a hundred and
    eighty good results, and throwing the column away to signal one crash is
    the failure 973ed45 and 6581660 were both about: a whole script missing
    from a column reads as "not applicable" rather than "this run crashed".

    So a crash makes the run red, and the column is still published saying
    which scripts are missing from it.
    """
    # exit_codes is {file: {column label: {script: rc}}} -- one file per
    # transport, one column per backend the leg compared.
    #
    # Keyed by file *and* script, not by script alone. A script runs once per
    # transport, so flattening on the name lets a clean vxi11 run overwrite a
    # crashed hislip one and the leg reports itself healthy. That is the exact
    # failure this file exists to prevent, and it got through the first CI run.
    flat: dict[str, int | None] = {}
    for filename, per_file in exit_codes.items():
        transport = filename.replace(".rc.json", "")
        for per_column in (per_file or {}).values():
            if isinstance(per_column, dict):
                for script, code in per_column.items():
                    flat[f"{script} [{transport}]"] = code

    errors = [
        {"script": s, "exit": rc} for s, rc in sorted(flat.items()) if rc in OUR_BUG
    ]
    flaky = any(rc == LOST_TARGET for rc in flat.values())
    if not reports:
        return "not-run", "the leg produced no report", flaky, errors
    reason = ""
    if errors:
        named = ", ".join(e["script"] for e in errors)
        reason = (
            f"{len(errors)} script(s) crashed and are missing from this "
            f"column: {named}. That is this suite's bug, not a finding about "
            f"the backend."
        )
    return "ok", reason, flaky, errors


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
    parser.add_argument(
        "--summary",
        help="append a Markdown tally to this file, for $GITHUB_STEP_SUMMARY. "
        "Written per leg as well as for the run, so a leg that failed is "
        "diagnosable without opening the aggregate",
    )
    args = parser.parse_args()

    leg = json.loads(args.leg)
    root = Path(args.dir)
    reports = aggregate.column_files(root) if root.is_dir() else []
    exit_codes: dict = {}
    for path in sorted(root.glob("*.rc.json")) if root.is_dir() else []:
        exit_codes[path.name] = json.loads(path.read_text(encoding="utf-8"))

    status, reason, flaky, errors = classify(reports, exit_codes)
    if args.status:
        status, reason = args.status, args.reason or reason
    elif args.reason:
        reason = args.reason

    leg.update(
        status=status,
        reason=reason,
        flaky=flaky,
        errors=errors,
        exit_codes=exit_codes,
        duration=round(args.duration, 1),
        host=f"{platform.system()} {platform.machine()}",
        reports=[p.name for p in reports],
    )
    Path(args.out).write_text(json.dumps(leg, indent=2) + "\n", encoding="utf-8")
    print(f"{leg.get('id', '?')}: {status}" + (f" -- {reason}" if reason else ""))

    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summarise(leg, reports))
    return 0


def summarise(leg: dict, reports: list[Path]) -> str:
    """This leg, in Markdown. Failures and skips by name, passes as a count.

    The names matter and the count does not: a report is read to find out what
    went wrong, and a list of two hundred passing checks buries the one that
    did not.
    """
    name = leg.get("label", leg.get("id", "?"))
    where = leg.get("os_label") or leg.get("host", "")
    out = [f"### {name} &mdash; {where}", ""]
    if leg["status"] != "ok":
        out += [f"**{leg['status']}** &mdash; {leg.get('reason', '')}", ""]
        return "\n".join(out) + "\n"
    if leg.get("errors"):
        out += [
            f"> **{len(leg['errors'])} script(s) crashed** and are missing from "
            f"this column: "
            + ", ".join(f"`{e['script']}` (exit {e['exit']})" for e in leg["errors"])
            + ". That is this suite's own bug, never a finding about the "
            "backend -- but the checks those scripts would have run are simply "
            "absent below, not passing.",
            "",
        ]

    for path in reports:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        columns = loaded if isinstance(loaded, list) else [loaded]
        for column in columns:
            results = column.get("results", [])
            tally: dict[str, int] = {}
            for result in results:
                tally[result["outcome"]] = tally.get(result["outcome"], 0) + 1
            out.append(
                f"`{path.stem}` &mdash; {tally.get('PASS', 0)} passed, "
                f"{tally.get('FAIL', 0)} failed, {tally.get('SKIP', 0)} skipped"
            )
            out.append("")
            for outcome in ("FAIL", "SKIP"):
                named = [r for r in results if r["outcome"] == outcome]
                if not named:
                    continue
                out.append(
                    f"<details><summary>{len(named)} {outcome}</summary>\n"
                )
                for result in named:
                    line = result["name"].replace("|", "\\|")
                    out.append(f"- {line}")
                out.append("\n</details>\n")
    if leg.get("flaky"):
        out += [
            "> The target went away at least once (exit 3). Not counted as a "
            "failure: a flaky bench reported as a library regression wastes "
            "the next person's afternoon.",
            "",
        ]
    out.append(f"_{leg.get('duration', 0)}s_\n")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
