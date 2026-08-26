# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared scaffolding for the reproducers.

Each reproducer is a statement about one finding, so they share only what
makes them runnable with no arguments: a mock server, a backend, and an exit
code that means "this still reproduces".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import backends  # noqa: E402
from testgear.server import mock_server  # noqa: E402


def parse(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--pyvisa-py",
        metavar="TREE",
        help="a pyvisa-py checkout to test instead of whatever is installed, "
        "so a candidate fix can be pointed at directly",
    )
    args = parser.parse_args()
    if args.pyvisa_py:
        try:
            backends.use_pyvisa_py_tree(args.pyvisa_py)
        except backends.TreeError as exc:
            print(f"--pyvisa-py: {exc}", file=sys.stderr)
            raise SystemExit(4)
    return args


def target(args):
    """The backend under test, with its provenance printed."""
    resolved = backends.resolve("py")
    if not resolved.available:
        print(f"pyvisa-py is not available: {resolved.reason}", file=sys.stderr)
        raise SystemExit(4)
    for key, value in backends.provenance(resolved).items():
        print(f"  {key}: {value}")
    return resolved


def verdict(reproduces: bool, description: str) -> int:
    """Report and return an exit code: non-zero while the finding stands."""
    if reproduces:
        print(f"\nSTILL REPRODUCES: {description}")
        return 1
    print(f"\nFIXED: {description}")
    return 0


__all__ = ["parse", "target", "verdict", "mock_server"]
