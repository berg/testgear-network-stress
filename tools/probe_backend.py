#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Does the backend actually load? Answered before anything is run against it.

    tools/probe_backend.py --backend keysight --pyvisa-py ../pyvisa-py

The Linux legs get this from docker/entrypoint.sh. This is the same check for
a runner with no container, and it keeps the same exit codes, because the
distinction they draw is the useful part:

    10  the library was not found
    11  it was found and would not initialise

A VISA library that is installed but cannot initialise fails at the first
viOpenDefaultRM, and pyvisa reports that as "backend not available", which is
indistinguishable from having forgotten to install it. On Windows the likely
cause is a driver that wants a reboot the runner cannot give it -- worth
saying, rather than discovering later as a column full of identical failures.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import backends  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", required=True)
    parser.add_argument("--pyvisa-py", help="the checkout under test")
    args = parser.parse_args()

    if args.pyvisa_py:
        try:
            backends.use_pyvisa_py_tree(args.pyvisa_py)
        except backends.TreeError as exc:
            print(f"  {exc}")
            return 4

    resolved = backends.resolve(args.backend)
    print(f"  resolve({args.backend!r}): available={resolved.available} "
          f"locator={resolved.locator}")
    if not resolved.available:
        print(f"  reason: {resolved.reason}")
        if resolved.spec.source:
            print(f"  get it from: {resolved.spec.source}")
        return 10

    try:
        rm = resolved.resource_manager()
        print(f"  ResourceManager opened: {rm}")
        try:
            print(f"  library: {rm.visalib.get_library_paths()}")
        except Exception:
            pass
    except Exception as exc:
        print(f"  FAILED to open a ResourceManager: {type(exc).__name__}: {exc}")
        print("  Found, but would not initialise. On a hosted Windows runner")
        print("  the usual cause is a driver that wants a reboot.")
        return 11

    for line in backends.provenance(resolved).items():
        print(f"  {line[0]}: {line[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
