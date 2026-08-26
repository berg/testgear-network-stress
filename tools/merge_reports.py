#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fold per-backend JSON reports into one matrix.

`compare.py` produces a matrix when it runs every backend itself. That is not
possible here: each vendor VISA lives in its own container, so the columns are
produced separately and have to be joined afterwards. Same rendering, same
stable-key matching, different source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="directory of <backend>.json reports")
    parser.add_argument("--html", metavar="PATH")
    parser.add_argument("--protocol", default="")
    args = parser.parse_args()

    columns = []
    # pyvisa-py first: it is the subject, and the eye reads left to right.
    for path in sorted(
        Path(args.directory).glob("*.json"),
        key=lambda p: (p.stem != "py", p.stem),
    ):
        loaded = json.loads(path.read_text())
        # compare.py writes a list of already-merged columns; a bare report is
        # a single one.
        for column in loaded if isinstance(loaded, list) else [loaded]:
            column.setdefault("label", path.stem)
            columns.append(column)

    if not columns:
        print(f"no reports in {args.directory}", file=sys.stderr)
        return 4

    keys: list[str] = []
    display: dict[str, str] = {}
    seen = set()
    for column in columns:
        for result in column.get("results", []):
            key = result.get("key", result["name"])
            if key not in seen:
                seen.add(key)
                keys.append(key)
                display[key] = result["name"]
    lookups = [
        {r.get("key", r["name"]): r for r in c.get("results", [])} for c in columns
    ]

    width = min(max((len(display[k]) for k in keys), default=10), 74)
    header = "  ".join(c["label"][:14].ljust(14) for c in columns)
    print(f"\n{'check'.ljust(width)}  {header}")
    print("-" * (width + 2 + len(header)))

    differing = 0
    for key in keys:
        cells, outcomes = [], set()
        for lookup in lookups:
            result = lookup.get(key)
            outcome = result["outcome"] if result else "-"
            outcomes.add(outcome)
            cells.append(outcome.ljust(14))
        differs = len(outcomes) > 1
        differing += differs
        print(f"{display[key][:width].ljust(width)}  {'  '.join(cells)}"
              f"{'<' if differs else ''}")

    print(
        f"\n{len(keys)} checks across {len(columns)} implementations, "
        f"{differing} where they disagree (marked <)"
    )

    if args.html:
        title = "VISA implementation comparison"
        if args.protocol:
            title += f" ({args.protocol})"
        report.write_matrix(columns, args.html, title=title)
        print(f"HTML matrix written to {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
