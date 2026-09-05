#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The verdict: is the suite itself broken?

    tools/ci_status.py --columns columns/

Neither runner's exit status can answer this. compare.py always exits 0 on
purpose -- disagreement is the finding, not an error -- and run_all exits with
a count of failed scripts, which conflates a check failing with the bench going
away. So the verdict is computed here, from the exit codes the legs recorded.

What is red:

  exit 2, 5   the suite is broken. 2 is an unexpected exception, 5 is this
              suite calling VISA wrongly. Never a finding about a backend.
  errored     a leg whose own setup failed, as recorded in its manifest.

What is not:

  exit 1      a check failed. That is the product. pyvisa-py fails a good
              many checks today and docs/findings.md is the list.
  exit 3      the target went away. Flagged, never fatal: a flaky bench
              reported as a library regression wastes the next person's
              afternoon, and a shared cloud runner is a flakier bench than a
              desk.
  exit 4      a backend is not installed. Reported as an unavailable column.

There is no baseline and no regression gate, deliberately. This suite exists
to report on VISA implementations, so a check that pyvisa-py newly fails is a
new finding, and the page is where it belongs -- not a red build on a pull
request to this repository, which cannot fix it. Whether pyvisa-py got better
or worse is a difference between two runs of the page.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUR_BUG = (2, 5)
LOST_TARGET = 3


def annotate(level: str, message: str) -> None:
    """A GitHub Actions annotation, and a plain line for a human terminal."""
    print(f"::{level}::{message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--columns", required=True)
    args = parser.parse_args()

    root = Path(args.columns)
    indexes: dict[str, list] = {}
    for protocol_dir in sorted(root.iterdir()):
        index_path = protocol_dir / "index.json"
        if protocol_dir.is_dir() and index_path.exists():
            indexes[protocol_dir.name] = json.loads(
                index_path.read_text(encoding="utf-8")
            )["columns"]

    verdict = 0
    flaky_legs: set[str] = set()

    # Exit codes are recorded per leg, covering every transport, and the same
    # leg appears in each protocol's index. Walk them once, keyed by leg and
    # script, or every crash is reported once per transport directory and
    # labelled with whichever one happened to be iterating.
    crashes: dict[tuple[str, str], int] = {}
    lost: dict[tuple[str, str], int] = {}
    for index in indexes.values():
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
    for protocol, index in indexes.items():
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

    if flaky_legs:
        annotate(
            "warning",
            f"the target went away at least once in: {', '.join(sorted(flaky_legs))}",
        )

    print(f"\nverdict: {'FAIL' if verdict else 'ok'}")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
