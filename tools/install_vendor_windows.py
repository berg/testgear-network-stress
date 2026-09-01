#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install a vendor VISA on Windows, from what the manifest says.

    tools/install_vendor_windows.py --install install.json --from vendor/

*** The silent-install arguments in the manifest are unverified. ***

Keysight IO Libraries Suite and TekVISA are InstallShield-family bundles.
Flags like `/quiet /norestart` and `/S /v/qn` are plausible and are not
confirmed here, so they are data in the manifest rather than code in this file:
correcting them is an `aws s3 cp`, not a pull request. Verify them by hand on a
throwaway Windows VM once, and write down what worked in docs/windows.md.

Two things this deliberately does not do.

It does not reboot. A hosted runner cannot: `shutdown /r` ends the job. Exit
3010 means "succeeded, reboot required", so it is treated as success, the
services the manifest names are started explicitly, and then the caller probes.
If the library still will not initialise, that is reported as an unavailable
column carrying the reason -- which is a legible page, not a broken run.

And it does not decide whether the install worked. Only the probe can answer
that, because "the installer exited 0" and "viOpenDefaultRM succeeds" are
different claims and the gap between them is exactly what goes wrong here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

#: MSI's "all done, but reboot before you rely on it".
REBOOT_REQUIRED = 3010


def run(argv: list[str]) -> int:
    print(f"$ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--install", required=True, help="the manifest's install block")
    parser.add_argument("--from", dest="src", default="vendor")
    args = parser.parse_args()

    spec = json.loads(Path(args.install).read_text(encoding="utf-8"))
    if not spec:
        print("no install block in the manifest for this backend", file=sys.stderr)
        return 4

    installers = sorted(Path(args.src).rglob("*.exe")) + sorted(
        Path(args.src).rglob("*.msi")
    )
    if not installers:
        print(f"no installer under {args.src}", file=sys.stderr)
        return 4
    installer = installers[0]

    argv = [str(installer), *spec.get("args", [])]
    if installer.suffix.lower() == ".msi":
        argv = ["msiexec", "/i", str(installer), *spec.get("args", [])]

    code = run(argv)
    ok = spec.get("success_exit_codes", [0, REBOOT_REQUIRED])
    if code not in ok:
        print(
            f"the installer exited {code}; the manifest accepts {ok}.\n"
            f"If this is the wrong silent-install invocation, fix it in the "
            f"bucket's manifest.json -- it is data for exactly this reason.",
            file=sys.stderr,
        )
        return 1
    if code == REBOOT_REQUIRED:
        print(
            "exit 3010: installed, wants a reboot. A hosted runner cannot give "
            "it one, so the services below are started by hand and the probe "
            "decides whether that was enough."
        )

    for service in spec.get("services", []):
        # Best effort. A service that will not start is not fatal here: the
        # probe is what decides, and it can say something more useful than
        # "net start failed".
        run(["powershell", "-NoProfile", "-Command",
             f"Start-Service -Name '{service}' -ErrorAction SilentlyContinue; "
             f"Get-Service -Name '{service}' | Format-List Name,Status"])

    library = spec.get("library")
    if library:
        present = Path(library).exists()
        print(f"{library}: {'present' if present else 'MISSING'}")
        if not present:
            print(
                "The installer reported success and the library is not there. "
                "That is the manifest describing a different package than the "
                "one in the bucket.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
