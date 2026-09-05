#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every check's outcome as one sorted line, so two runs can be diffed.

    tools/outcomes.py reports/                      # one line per check
    tools/outcomes.py reports/ --against before/    # what changed, and how
    tools/outcomes.py site/columns > after.txt      # a CI run's columns

"Did my branch change anything?" is a question about two runs of the same
suite, and the reports answer it badly: they carry durations, measured rates,
commit hashes and resource strings, so `diff` on them is noise. This strips a
run to what the question is about -- which checks ran and how each one came
out -- in an order that does not depend on the suite's, and with the
measurements left in the reports where they belong.

Takes whatever a run leaves behind: the per-script JSON that `run_all.py
--reports` writes, the merged columns from `compare.py --json`, a column file
from a CI artifact, or a `columns/` tree from the aggregate. Per-script
reports in one directory are folded into one column, exactly as the matrix
folds them, so the keys match across every source.

`--against` pairs columns by label and names each change for what it is: a
check that now fails or now skips where it passed is a regression, a skip that
counts because at a glance it reads like a pass; a check that now passes is a
fix; a check that appeared or vanished is a change to the suite, not to the
implementation, and is listed apart so it is not mistaken for either. The exit
status is 0 whatever changed. This reports; it does not judge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from testgear import aggregate  # noqa: E402

from artifact_matrix import load  # noqa: E402

REGRESSIONS = {("PASS", "FAIL"), ("PASS", "SKIP")}


def _is_script_report(loaded) -> bool:
    return isinstance(loaded, dict) and "script" in loaded


def _column_label(column: dict, fallback: str) -> str:
    return column.get("label") or column.get("context", {}).get("backend") or fallback


def load_columns(path: Path) -> list[dict]:
    """Every column a path holds, whatever shape it was left in."""
    if path.is_dir():
        if (path / "index.json").exists():
            return load(path)
        subdirs = [d for d in sorted(path.iterdir()) if (d / "index.json").exists()]
        if subdirs:
            # The aggregate's columns/: one directory per transport. Every
            # transport's column for one implementation is the same
            # implementation, so they fold into one column per label. The
            # keys already carry the transport in the script's title.
            by_label: dict[str, dict] = {}
            for sub in subdirs:
                for column in load(sub):
                    label = _column_label(column, sub.name)
                    merged = by_label.setdefault(
                        label, {"label": label, "results": [], "status": "not-run"}
                    )
                    if aggregate.status(column) == "ok":
                        merged["status"] = "ok"
                        merged["results"].extend(column.get("results", []))
            return list(by_label.values())
        files = aggregate.column_files(path)
        if not files:
            raise SystemExit(f"{path}: no reports here")
        columns: list[dict] = []
        scripts: list[dict] = []
        for file in files:
            loaded = json.loads(file.read_text(encoding="utf-8"))
            if _is_script_report(loaded):
                scripts.append(loaded)
            else:
                columns.extend(_columns_of(loaded, file))
        if scripts:
            label = _column_label({"context": scripts[0].get("context", {})}, path.name)
            columns.append(aggregate.merge(scripts, label))
        return columns

    loaded = json.loads(path.read_text(encoding="utf-8"))
    if _is_script_report(loaded):
        label = _column_label(loaded, path.stem)
        return [aggregate.merge([loaded], label)]
    return _columns_of(loaded, path)


def _columns_of(loaded, file: Path) -> list[dict]:
    """compare.py writes a list of merged columns; a CI artifact, a list of
    one; merge_reports accepts a bare column dict. All of those."""
    items = loaded if isinstance(loaded, list) else [loaded]
    for column in items:
        column.setdefault("label", _column_label(column, file.stem))
    return items


def outcomes(column: dict) -> dict[str, str]:
    """Check key to outcome, joined on the same key the matrix joins on."""
    return {
        r.get("key", r["name"]): r["outcome"]
        for r in column.get("results", [])
    }


def load_run(paths: list[str]) -> dict[str, dict[str, str]]:
    """Label to outcomes, across every path given."""
    run: dict[str, dict[str, str]] = {}
    for raw in paths:
        for column in load_columns(Path(raw)):
            label = aggregate.label(column)
            if aggregate.status(column) != "ok":
                # A column that did not run has nothing to diff. Present and
                # empty, so a run where it died shows every check as gone
                # rather than showing nothing at all.
                run.setdefault(label, {})
                continue
            run.setdefault(label, {}).update(outcomes(column))
    return run


def render(run: dict[str, dict[str, str]]) -> str:
    lines: list[str] = []
    for label in sorted(run):
        lines.append(f"== {label} ==")
        for key in sorted(run[label]):
            lines.append(f"{run[label][key]}\t{key}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def compare(
    now: dict[str, dict[str, str]], before: dict[str, dict[str, str]]
) -> tuple[list[str], int]:
    lines: list[str] = []
    changed = 0
    for label in sorted(set(now) | set(before)):
        if label not in before:
            lines.append(f"== {label}: not in the earlier run ==")
            continue
        if label not in now:
            lines.append(f"== {label}: not in this run ==")
            continue
        a, b = before[label], now[label]
        regressed = [(k, a[k], b[k]) for k in sorted(a) if k in b and (a[k], b[k]) in REGRESSIONS]
        fixed = [k for k in sorted(a) if k in b and a[k] != "PASS" and b[k] == "PASS"]
        other = [
            (k, a[k], b[k])
            for k in sorted(a)
            if k in b and a[k] != b[k] and (a[k], b[k]) not in REGRESSIONS and b[k] != "PASS"
        ]
        appeared = [k for k in sorted(b) if k not in a]
        vanished = [k for k in sorted(a) if k not in b]
        changed += len(regressed) + len(fixed) + len(other) + len(appeared) + len(vanished)

        lines.append(f"== {label} ==")
        for title, rows in (
            ("regressed", [f"{x} -> {y}\t{k}" for k, x, y in regressed]),
            ("fixed", [f"PASS\t{k}" for k in fixed]),
            ("changed", [f"{x} -> {y}\t{k}" for k, x, y in other]),
            ("new checks", [f"{b[k]}\t{k}" for k in appeared]),
            ("gone", [f"{a[k]}\t{k}" for k in vanished]),
        ):
            if rows:
                lines.append(f"-- {title} ({len(rows)})")
                lines.extend(rows)
        summary = (
            f"{len(regressed)} regressed, {len(fixed)} fixed, {len(other)} changed "
            f"otherwise, {len(appeared)} new, {len(vanished)} gone; "
            f"{len(set(a) & set(b))} checks in both"
        )
        lines.append(summary)
        lines.append("")
    return lines, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="+",
        help="report files or directories: run_all --reports, compare --json, "
        "a CI column, or the aggregate's columns/",
    )
    parser.add_argument(
        "--against",
        nargs="+",
        metavar="PATH",
        help="an earlier run in any of the same shapes; print what changed "
        "instead of the outcomes themselves",
    )
    parser.add_argument("--out", help="write here instead of stdout")
    args = parser.parse_args()

    now = load_run(args.paths)
    if not now:
        print("no columns found", file=sys.stderr)
        return 4

    if args.against:
        lines, changed = compare(now, load_run(args.against))
        text = "\n".join(lines).rstrip("\n") + "\n"
    else:
        text = render(now)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        total = sum(len(v) for v in now.values())
        print(f"{args.out}: {total} outcomes across {len(now)} column(s)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
