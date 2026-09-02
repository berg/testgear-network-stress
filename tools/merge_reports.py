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

from testgear import aggregate, report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="directory of <backend>.json reports")
    parser.add_argument("--html", metavar="PATH")
    parser.add_argument("--protocol", default="")
    args = parser.parse_args()

    columns = []
    # pyvisa-py first: it is the subject, and the eye reads left to right.
    for path in sorted(
        aggregate.column_files(args.directory),
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

    matrix = aggregate.build(columns)
    width = min(max((len(r.name) for r in matrix.rows), default=10), 74)
    header = "  ".join(c["label"][:14].ljust(14) for c in columns)
    print(f"\n{'check'.ljust(width)}  {header}")
    print("-" * (width + 2 + len(header)))

    for row in matrix.rows:
        cells = "  ".join(o.ljust(14) for o in row.outcomes)
        print(f"{row.name[:width].ljust(width)}  {cells}{'<' if row.differs else ''}")

    print(
        f"\n{len(matrix.rows)} checks across {len(columns)} implementations, "
        f"{len(matrix.disagreements)} where they disagree (marked <)"
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
