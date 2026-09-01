#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fold downloaded leg artifacts into one canonical set of columns.

    tools/normalise_reports.py --in artifacts/ --out columns/ --plan legs.json

Legs write reports in whichever shape their runner produced: `compare.py --json`
writes a merged column per transport, `run_all.py --reports` writes one file per
script. Both arrive here and leave in the same shape.

The index this writes is built from the **plan**, not from what turned up. A leg
that was meant to run and produced nothing still gets an entry carrying its
reason, so the page draws the column and says what happened to it. Building the
index from the artifacts instead would make a dead leg indistinguishable from
one that was never asked for -- and a comparison table with a column silently
absent reads like agreement between the backends that remain.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import aggregate  # noqa: E402

PROTOCOLS = ("hislip", "vxi11")


def load_column(paths: list[Path], label: str) -> dict | None:
    """One column from whatever shape a leg wrote.

    A JSON list is `compare.py`'s already-merged column. Anything else is a
    per-script report and has to go through merge(), which prefixes names and
    keys with the script -- two scripts can legitimately word a check the same
    way, and collapsing them would compare unrelated things.
    """
    merged: list[dict] = []
    for path in paths:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            for column in loaded:
                column.setdefault("label", label)
                merged.append(column)
        else:
            merged.append(aggregate.merge([loaded], label))
    if not merged:
        return None
    if len(merged) == 1:
        return merged[0]

    # Several per-script reports: one column, results concatenated in the
    # order the scripts ran.
    out = dict(merged[0])
    out["results"] = [r for c in merged for r in c.get("results", [])]
    out["notes"] = [n for c in merged for n in c.get("notes", [])]
    return out


def leg_reports(root: Path, protocol: str) -> list[Path]:
    """The report files in a leg's artifact that belong to one transport."""
    exact = root / f"{protocol}.json"
    if exact.exists():
        return [exact]
    # run_all.py's naming: <script>-<protocol>.json, in suite order, which is
    # the order the filenames sort in only by accident -- so sort on the script
    # list rather than on the name.
    from testgear import suite

    order = {s.name[:-3]: i for i, s in enumerate(suite.SCRIPTS)}
    found = [
        p
        for p in aggregate.column_files(root)
        if p.stem.endswith(f"-{protocol}")
    ]
    return sorted(found, key=lambda p: order.get(p.stem[: -len(protocol) - 1], 999))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    parser.add_argument("--plan", required=True, help="the legs JSON")
    parser.add_argument(
        "--raw",
        help="copy each leg's untouched output here, so a row on the page can "
        "be chased back to the report it came from",
    )
    args = parser.parse_args()

    raw = Path(args.plan).read_text(encoding="utf-8").strip()
    if not raw:
        # The plan job failed, and the aggregate runs with `if: always()`. Say
        # which one broke: a JSONDecodeError here points at this file and the
        # actual fault is two jobs upstream.
        print(
            f"{args.plan} is empty: the plan job produced no legs, so there is "
            f"nothing to normalise. Look at that job, not this one.",
            file=sys.stderr,
        )
        return 4
    plan = json.loads(raw)
    if not plan:
        print("the plan lists no legs", file=sys.stderr)
        return 4
    src, dst = Path(args.src), Path(args.dst)

    for protocol in PROTOCOLS:
        index: list[dict] = []
        out_dir = dst / protocol
        out_dir.mkdir(parents=True, exist_ok=True)

        for leg in plan:
            entry = {
                "id": leg["id"],
                "backend": leg["backend"],
                "label": leg.get("label", leg["id"]),
                "os_label": leg.get("os_label", ""),
                "order": leg.get("order", 0),
                "file": f"{leg['id']}.json",
            }
            # Artifacts land as <download root>/reports-<leg id>/.
            root = src / f"reports-{leg['id']}"
            status_path = root / "leg.json"
            status = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.exists()
                else {}
            )
            if not root.is_dir():
                # No artifact at all: the job did not even reach its upload
                # step. Said plainly rather than left as an absence.
                entry.update(
                    status="not-run",
                    reason="the job produced no artifact",
                )
                index.append(entry)
                continue

            # Carried through so the verdict has one input. exit codes are
            # the only thing that separates "a check failed", which is the
            # product, from "the suite is broken", which is not.
            for key in ("vendor_version", "flaky", "exit_codes", "host"):
                if status.get(key):
                    entry[key] = status[key]

            column = None
            if status.get("status", "ok") == "ok":
                column = load_column(leg_reports(root, protocol), entry["label"])

            if column is None:
                entry.update(
                    status=status.get("status", "not-run") if status else "not-run",
                    reason=status.get("reason")
                    or f"no {protocol} report in the artifact",
                )
                if entry["status"] == "ok":
                    # It said it was fine and then produced nothing for this
                    # transport. Do not let "ok" stand: it would be compared
                    # as an empty column and read as total disagreement.
                    entry["status"] = "not-run"
            else:
                column["status"] = "ok"
                column["backend"] = entry["backend"]
                column["os_label"] = entry["os_label"]
                column["id"] = entry["id"]
                if entry.get("vendor_version"):
                    column["vendor_version"] = entry["vendor_version"]
                (out_dir / entry["file"]).write_text(
                    json.dumps(column, indent=2), encoding="utf-8"
                )
                entry["status"] = "ok"
            index.append(entry)

        (out_dir / "index.json").write_text(
            json.dumps({"columns": index}, indent=2) + "\n", encoding="utf-8"
        )
        ran = sum(1 for e in index if e["status"] == "ok")
        print(f"{protocol}: {ran}/{len(index)} columns")
        for entry in index:
            if entry["status"] != "ok":
                print(f"  {entry['label']} ({entry['id']}): "
                      f"{entry['status']} -- {entry.get('reason', '')}")

    if args.raw:
        raw = Path(args.raw)
        raw.mkdir(parents=True, exist_ok=True)
        for leg_dir in sorted(src.glob("reports-*")):
            shutil.copytree(leg_dir, raw / leg_dir.name, dirs_exist_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
