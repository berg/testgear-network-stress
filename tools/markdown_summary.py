#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The run, as Markdown, for a GitHub step summary.

    tools/markdown_summary.py --columns columns/ --out summary.md \\
        --title "Full run" --page-url https://berg.github.io/...

Read on a phone as often as on a desk, so it is ordered by what someone needs
first: what each implementation did, then the failures that are findings, then
the disagreements, and only then the full grid -- folded away, because forty
green rows above the one red one buries its own point.

$GITHUB_STEP_SUMMARY is capped at 1 MiB and truncates *silently*, so the tables
are capped here instead and the untruncated copy goes to a file that the page
links.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import aggregate  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_matrix import load  # noqa: E402

#: GitHub's cap, less room for whatever the workflow appends around this.
STEP_SUMMARY_LIMIT = 900_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--columns", required=True, help="the normalised columns/")
    parser.add_argument("--out", required=True)
    parser.add_argument("--title", default="VISA conformance")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--page-url", default="")
    parser.add_argument(
        "--step-summary",
        help="also write a copy capped to fit $GITHUB_STEP_SUMMARY",
    )
    parser.add_argument("--max-rows", type=int, default=60)
    args = parser.parse_args()

    root = Path(args.columns)
    parts: list[str] = [f"# {args.title}", ""]
    if args.subtitle:
        parts += [args.subtitle, ""]

    protocols = [d.name for d in sorted(root.iterdir()) if d.is_dir()]
    if not protocols:
        print(f"no protocol directories in {root}", file=sys.stderr)
        return 4

    provenance: dict = {}
    for protocol in protocols:
        columns = load(root / protocol)
        if not columns:
            continue
        matrix = aggregate.build(columns)
        if not provenance:
            provenance = next(
                (c.get("context", {}) for c in columns
                 if aggregate.status(c) == "ok"),
                {},
            )
        parts.append(
            aggregate.render_markdown(matrix, protocol, max_rows=args.max_rows)
        )

    if provenance:
        parts += [
            "## What was tested",
            "",
            *[
                f"- **{k}** &mdash; {v}"
                for k, v in provenance.items()
                if k in ("pyvisa", "pyvisa-py commit", "python", "platform")
            ],
            "",
        ]

    if args.page_url:
        parts += [f"[The full matrix, with every failure's detail]({args.page_url})", ""]

    text = "\n".join(parts)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{args.out}: {len(text)} bytes")

    if args.step_summary:
        capped = text
        if len(capped) > STEP_SUMMARY_LIMIT:
            # Truncate here rather than letting GitHub do it, because GitHub
            # does it without saying so -- and a summary that stops mid-table
            # with no explanation reads as a crash.
            capped = capped[:STEP_SUMMARY_LIMIT] + (
                "\n\n_Truncated to fit the step summary. "
                "The full report is in this run's artifacts._\n"
            )
        Path(args.step_summary).write_text(capped, encoding="utf-8")
        print(f"{args.step_summary}: {len(capped)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
