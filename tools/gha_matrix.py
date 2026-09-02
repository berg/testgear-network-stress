#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""What to fan out to, as JSON, for a GitHub Actions matrix.

The fan-out is data rather than YAML for two reasons. It reads labels and
install pointers out of `testgear.backends.BACKENDS`, so there is still one
table saying what a backend is called and where you get it. And `runs_on` is a
field: moving a Windows leg from a hosted runner to a bench box with the driver
already installed is an edit here, not a workflow rewrite.

    tools/gha_matrix.py --run-type full
    tools/gha_matrix.py --run-type pyvisa-py --github-output

Protocol is deliberately not an axis. A leg sweeps both transports in one job:
the mock server is per-script and ephemeral, the image is the expensive part,
and splitting would double the image builds to save nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import backends  # noqa: E402

#: One entry per leg, in the order their columns appear on the page.
#:
#: `image` names the Dockerfile BACKEND build-arg for a Linux leg. The `py`
#: image installs no vendor library, which is what lets pyvisa-py's column come
#: from an image built exactly like the vendors' -- same base, same Python --
#: so a difference between columns is a difference between implementations.
#:
#: `vendor` is the gate: a leg with vendor=True needs a non-redistributable
#: installer, so it only ever runs where an AWS role is reachable, which is
#: never on the pull_request path.
#:
#: Every leg is Linux, and that is the point rather than a limitation. All four
#: implementations run in the same image family, on the same kernel, against
#: the same mock -- so a difference between two columns is a difference between
#: two implementations and not between two machines. Keysight ships a 64-bit
#: Linux build, which is what makes a third vendor column possible here;
#: TekVISA does not, and is left to whoever runs it by hand.
LEGS: tuple[dict, ...] = (
    {"id": "linux-py", "backend": "py", "runner": "linux",
     "runs_on": "ubuntu-latest", "image": "py", "os_label": "Linux",
     "vendor": False, "full_only": False},
    {"id": "linux-ni", "backend": "ni", "runner": "linux",
     "runs_on": "ubuntu-latest", "image": "ni", "os_label": "Linux",
     "vendor": True, "full_only": True},
    {"id": "linux-rs", "backend": "rs", "runner": "linux",
     "runs_on": "ubuntu-latest", "image": "rs", "os_label": "Linux",
     "vendor": True, "full_only": True},
    {"id": "linux-keysight", "backend": "keysight", "runner": "linux",
     "runs_on": "ubuntu-latest", "image": "keysight", "os_label": "Linux",
     "vendor": True, "full_only": True},
)


def plan(run_type: str) -> list[dict]:
    """The legs for a run, in column order.

    A pyvisa-py run gets no vendor leg at all. That is the security boundary,
    not a convenience: such a run clones a caller-named repository at a
    caller-named ref and executes it, so it must never be in a position to hold
    a credential.
    """
    legs = []
    for order, leg in enumerate(LEGS):
        if run_type != "full" and leg["full_only"]:
            continue
        spec = backends.BACKENDS[leg["backend"]]
        legs.append(
            {
                **leg,
                "order": order,
                "label": spec.name,
                "source": spec.source,
            }
        )
    return legs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run-type", choices=("full", "pyvisa-py"), default="pyvisa-py"
    )
    parser.add_argument(
        "--runner",
        choices=("linux",),
        help="only the legs for one runner OS",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="append legs and linux-legs to $GITHUB_OUTPUT",
    )
    args = parser.parse_args()

    legs = plan(args.run_type)
    if args.runner:
        legs = [leg for leg in legs if leg["runner"] == args.runner]

    if not args.github_output:
        print(json.dumps(legs, indent=2))
        return 0

    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print("--github-output outside Actions: no $GITHUB_OUTPUT", file=sys.stderr)
        return 4
    with open(out, "a", encoding="utf-8") as handle:
        for name, subset in (
            ("legs", legs),
            ("linux-legs", [l for l in legs if l["runner"] == "linux"]),
        ):
            handle.write(f"{name}={json.dumps(subset)}\n")
    print(json.dumps(legs, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
