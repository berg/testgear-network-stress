#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the same checks against several VISA implementations and diff them.

A check that fails everywhere is a hard problem or a wrong check. A check that
fails on one implementation and passes on another is a *disparity*, and that is
a much stronger claim: not "this behaviour is undesirable" but "this behaviour
is inconsistent with a shipping implementation of the same spec". That is the
argument that moves an upstream discussion, and producing it is the only reason
this script exists.

    ./compare.py                                  # every backend installed here
    ./compare.py --backends py,ni --protocol vxi11
    ./compare.py --backends py --pyvisa-py-trees main=/a,branch=/b
    ./compare.py --html comparison.html

Backends that are not installed are reported and skipped, never quietly
dropped: a matrix with a column silently absent reads like agreement between
the columns that remain.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from testgear import backends, report  # noqa: E402

HERE = Path(__file__).resolve().parent
CHECKS = HERE / "checks"

#: The scripts worth comparing. The soak is left out on purpose: it is a
#: randomised workload whose value is duration, and its single summary check
#: says nothing useful in a matrix.
DEFAULT_SCRIPTS = (
    "01_smoke.py",
    "02_io.py",
    "conformance.py",
    "05_lock.py",
    "09_remote_local.py",
)

VXI11_ONLY = ("vxi11_conformance.py",)


def run_one(
    python: str,
    script: str,
    protocol: str,
    backend: str,
    tree: str | None,
    extra: list[str],
    timeout: float,
) -> dict | None:
    """Run one script and return its report, or None if it produced none."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        argv = [
            python,
            str(CHECKS / script),
            "--protocol", protocol,
            "--backend", backend,
            "--report", str(out),
            *extra,
        ]
        if tree:
            argv += ["--pyvisa-py", tree]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            print(f"    {script}: timed out after {timeout:.0f}s", file=sys.stderr)
            return None
        if not out.exists():
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            why = first[-1] if first else f"exit {proc.returncode}"
            print(f"    {script}: no report ({why})", file=sys.stderr)
            return None
        return json.loads(out.read_text())


def merge(reports: list[dict], label: str) -> dict:
    """Fold several script reports into one column.

    Check names are prefixed with their script, because two scripts can
    legitimately use the same wording for different checks and collapsing them
    in the matrix would compare unrelated things.
    """
    merged: dict = {"label": label, "results": [], "notes": [], "context": {}}
    for rep in reports:
        script = rep.get("script", "?")
        if not merged["context"]:
            merged["context"] = dict(rep.get("context", {}))
        for result in rep.get("results", []):
            entry = dict(result)
            entry["name"] = f"{script}: {result['name']}"
            # Match on the masked key, display the full name. A check whose
            # message carries its measurements would otherwise split into one
            # row per backend, each showing a gap where the others answered.
            entry["key"] = f"{script}: {result.get('key', result['name'])}"
            merged["results"].append(entry)
        merged["notes"].extend(rep.get("notes", []))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--backends",
        default=None,
        help="comma-separated backend ids (default: every one installed here)",
    )
    parser.add_argument(
        "--pyvisa-py-trees",
        default=None,
        help="comma-separated label=path pairs, to compare checkouts of "
        "pyvisa-py against each other rather than different VISA libraries",
    )
    parser.add_argument("--protocol", default="vxi11", choices=("hislip", "vxi11"))
    parser.add_argument(
        "--scripts",
        default=None,
        help=f"comma-separated scripts (default: {','.join(DEFAULT_SCRIPTS)})",
    )
    parser.add_argument("--html", metavar="PATH", help="write the matrix as HTML")
    parser.add_argument("--json", metavar="PATH", help="write the merged reports")
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="per-script timeout in seconds"
    )
    parser.add_argument(
        "--python",
        default=str(HERE / ".venv" / "bin" / "python"),
        help="interpreter to run the checks with",
    )
    args, extra = parser.parse_known_args()

    scripts = list(
        args.scripts.split(",") if args.scripts else DEFAULT_SCRIPTS
    )
    if args.protocol == "vxi11" and not args.scripts:
        scripts += list(VXI11_ONLY)

    # Two comparison modes. Several trees of one backend answers "did my branch
    # change anything?"; several backends answers "is pyvisa-py the odd one
    # out?". They are the same matrix with different columns.
    columns: list[tuple[str, str, str | None]] = []
    if args.pyvisa_py_trees:
        for pair in args.pyvisa_py_trees.split(","):
            label, _, path = pair.partition("=")
            if not path:
                print(f"--pyvisa-py-trees wants label=path, got {pair!r}", file=sys.stderr)
                return 4
            try:
                backends.use_pyvisa_py_tree(path)
            except backends.TreeError as exc:
                print(f"{label}: {exc}", file=sys.stderr)
                return 4
            columns.append((label, "py", path))
    else:
        wanted = (
            args.backends.split(",") if args.backends else list(backends.BACKENDS)
        )
        for spec_id in wanted:
            resolved = backends.resolve(spec_id)
            if not resolved.available:
                print(f"skipping {spec_id}: {resolved.reason}")
                if resolved.spec.source:
                    print(f"  get it from: {resolved.spec.source}")
                continue
            if not resolved.spec.networked:
                print(
                    f"skipping {spec_id}: {resolved.name} does not speak network "
                    f"protocols, so it cannot reach the mock server"
                )
                continue
            columns.append((resolved.name, spec_id, None))

    if not columns:
        print("nothing to compare", file=sys.stderr)
        return 4
    if len(columns) == 1:
        print(
            f"only one column ({columns[0][0]}): this will produce a report, "
            f"not a comparison. Install another VISA to get a disparity out of it."
        )

    merged_columns = []
    for label, backend, tree in columns:
        print(f"\n=== {label} ({args.protocol})")
        reports = []
        for script in scripts:
            print(f"  {script}")
            rep = run_one(
                args.python, script, args.protocol, backend, tree, extra, args.timeout
            )
            if rep is not None:
                reports.append(rep)
        if reports:
            merged_columns.append(merge(reports, label))

    if not merged_columns:
        print("no reports were produced", file=sys.stderr)
        return 2

    # -- the matrix ---------------------------------------------------------
    keys: list[str] = []
    display: dict[str, str] = {}
    seen = set()
    for column in merged_columns:
        for result in column["results"]:
            key = result["key"]
            if key not in seen:
                seen.add(key)
                keys.append(key)
                display[key] = result["name"]
    lookups = [{r["key"]: r for r in c["results"]} for c in merged_columns]

    width = max((len(display[k]) for k in keys), default=10)
    width = min(width, 78)
    header = "  ".join(c["label"][:12].ljust(12) for c in merged_columns)
    print(f"\n{'check'.ljust(width)}  {header}")
    print("-" * (width + 2 + len(header)))

    disagreements = 0
    for key in keys:
        cells = []
        outcomes = set()
        for lookup in lookups:
            result = lookup.get(key)
            outcome = result["outcome"] if result else "-"
            outcomes.add(outcome)
            cells.append(outcome.ljust(12))
        differs = len(outcomes) > 1
        disagreements += differs
        marker = "<" if differs else " "
        print(f"{display[key][:width].ljust(width)}  {'  '.join(cells)}{marker}")

    print(
        f"\n{len(keys)} checks, {disagreements} where the implementations "
        f"disagree (marked <)"
    )

    if args.json:
        Path(args.json).write_text(json.dumps(merged_columns, indent=2))
        print(f"merged reports written to {args.json}")
    if args.html:
        report.write_matrix(
            merged_columns,
            args.html,
            title=f"Backend comparison ({args.protocol})",
        )
        print(f"HTML matrix written to {args.html}")

    # Disagreement is the finding, not an error: exit 0 so this can be run in
    # a pipeline without a disparity failing the build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
