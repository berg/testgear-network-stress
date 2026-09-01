#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Put the vendor installers in the bucket, and write the manifest.

    tools/upload_vendor.py --bucket "$VENDOR_BUCKET" --dry-run
    tools/upload_vendor.py --bucket "$VENDOR_BUCKET"

Run by a human with their own credentials. The CI role cannot do this: it has
no PutObject, deliberately, because a job that could replace a driver would
silently change what every future run measures.

By default it scans `vendor/` -- the same directory `remote-compare.sh` and the
Dockerfile already read -- so the local layout and the bucket layout stay the
same shape and there is one place to drop a download.

It reads the existing manifest first and merges, so uploading one new driver
does not disturb the others, and re-running with nothing changed is a no-op
apart from re-stating the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MANIFEST_KEY = "manifest.json"

#: What counts as an installer for each backend, and what to do with it.
#:
#: The R&S rule is not a wildcard on purpose. They also ship an armhf build for
#: the Raspberry Pi, and a glob that picked it up would fail inside the image
#: with a dpkg architecture error rather than here with an obvious one --
#: vendor/README.md warns about the same trap.
RULES: dict[tuple[str, str], dict] = {
    ("ni", "linux"): {
        "match": lambda p: p.suffix in (".zip", ".deb"),
        "reject": lambda p: "arm" in p.name.lower(),
        "install": {"kind": "apt-repo-deb", "package": "ni-visa"},
        "version": lambda p: _first(
            [(r"(20\d\d)Q([1-4])", lambda m: f"NI-VISA {m.group(1)} Q{m.group(2)}")],
            p.name,
        ),
    },
    ("rs", "linux"): {
        "match": lambda p: p.suffix == ".deb" and "amd64" in p.name,
        "reject": lambda p: "amd64" not in p.name,
        "install": {"kind": "deb"},
        "version": lambda p: _first(
            [(r"rsvisa[_-]([0-9.]+)", lambda m: f"R&S VISA {m.group(1)}")], p.name
        ),
    },
    ("keysight", "linux"): {
        "match": lambda p: p.suffix in (".gz", ".tgz", ".deb", ".run"),
        "reject": lambda p: "arm" in p.name.lower() or "i386" in p.name,
        # A record of what docker/Dockerfile does with it, not something read
        # back at build time -- the Dockerfile installs this one itself. Kept
        # here so the bucket says what the flags are without needing the repo.
        "install": {
            "kind": "bitrock-run",
            "args": ["--mode", "unattended", "--unattendedmodeui", "none"],
            "enable_components": "iio,iom,lan,interfaces",
            "library": "/opt/keysight/iolibs/libktvisa32.so",
            "applied_by": "docker/Dockerfile",
        },
        # IOLibrariesSuite-21.3.94-linux-x64.run. The marketing year (2026)
        # is not in the filename -- `--version` reports "Keysight IO Libraries
        # Suite 2026 21.3.94" -- so record the build, which is the part that
        # identifies what actually ran.
        "version": lambda p: _first(
            [
                (
                    r"IOLibrariesSuite-([0-9][0-9.]*)-",
                    lambda m: f"Keysight IO Libraries {m.group(1)}",
                ),
                (r"(20\d\d)", lambda m: f"Keysight IO Libraries {m.group(1)}"),
            ],
            p.name,
        ),
    },
}

#: TekVISA is Windows-only and has no place in a Linux matrix. `backends.py`
#: still knows about it, so `--backend tek` works for anyone running the suite
#: by hand on Windows; it is simply not something CI can reach.


def _first(patterns, text: str) -> str:
    for pattern, render in patterns:
        match = re.search(pattern, text)
        if match:
            return render(match)
    return ""


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def discover(root: Path) -> list[tuple[str, str, Path, dict]]:
    """Every installer under `root`, with the rule that claimed it."""
    found = []
    for (backend, os_name), rule in RULES.items():
        directory = root / backend
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name == "README.md":
                continue
            if rule["reject"](path):
                print(f"  ignoring {path.name}: not the architecture this runs on")
                continue
            if not rule["match"](path):
                print(f"  ignoring {path.name}: not an installer this knows")
                continue
            found.append((backend, os_name, path, rule))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--from", dest="root", default="vendor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be uploaded and write the manifest locally",
    )
    parser.add_argument(
        "--vendor-version",
        action="append",
        default=[],
        metavar="BACKEND=VERSION",
        help="state a version the filename does not carry, e.g. "
        "--vendor-version 'keysight=Keysight IO Libraries 2026'. It is what "
        "the published page labels the column with, so a wrong one is a page "
        "that names the wrong library",
    )
    parser.add_argument(
        "--manifest-out",
        help="also write the manifest here, for review before it goes up",
    )
    args = parser.parse_args()

    overrides: dict[str, str] = {}
    for pair in args.vendor_version:
        backend, _, version = pair.partition("=")
        if not version:
            print(f"--vendor-version wants BACKEND=VERSION, got {pair!r}",
                  file=sys.stderr)
            return 4
        overrides[backend] = version

    root = Path(args.root)
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 4

    print(f"scanning {root}/")
    found = discover(root)
    if not found:
        print(
            f"nothing to upload. Drop the installers in {root}/<backend>/ -- "
            f"see {root}/README.md",
            file=sys.stderr,
        )
        return 3

    s3 = None
    if not args.dry_run:
        import boto3

        s3 = boto3.client("s3")

    # Merge rather than replace: uploading one driver must not drop the others.
    manifest: dict = {"version": 1, "artifacts": []}
    if s3 is not None:
        try:
            body = s3.get_object(Bucket=args.bucket, Key=MANIFEST_KEY)["Body"].read()
            manifest = json.loads(body.decode("utf-8"))
            print(f"merging into the existing manifest "
                  f"({len(manifest.get('artifacts', []))} entries)")
        except Exception:
            print("no manifest in the bucket yet; starting one")

    by_key = {
        (a["backend"], a["os"]): a for a in manifest.get("artifacts", [])
    }

    for backend, os_name, path, rule in found:
        key = f"drivers/{backend}/{os_name}/{path.name}"
        sha = digest(path)
        size = path.stat().st_size
        entry = dict(by_key.get((backend, os_name), {}))
        entry.update(
            backend=backend,
            os=os_name,
            arch="x86_64",
            key=key,
            sha256=sha,
            bytes=size,
            vendor_version=(
                overrides.get(backend)
                or entry.get("vendor_version")
                or rule["version"](path)
            ),
            install=entry.get("install") or rule["install"],
        )
        entry["dest"] = f"vendor/{backend}/"
        by_key[(backend, os_name)] = entry

        print(f"\n{backend}/{os_name}: {path.name}")
        print(f"  {size / 1e6:.1f} MB  sha256 {sha[:16]}...")
        print(f"  -> s3://{args.bucket}/{key}")
        print(f"  version: {entry['vendor_version'] or '(unknown -- edit the manifest)'}")
        if entry["install"].get("verified") is False:
            print("  install args are UNVERIFIED; see docs/windows.md")
        if not args.dry_run:
            s3.upload_file(str(path), args.bucket, key)
            print("  uploaded")

    manifest["artifacts"] = [by_key[k] for k in sorted(by_key)]
    text = json.dumps(manifest, indent=2, sort_keys=False) + "\n"

    if args.manifest_out or args.dry_run:
        out = Path(args.manifest_out or "manifest.json")
        out.write_text(text, encoding="utf-8")
        print(f"\nmanifest written to {out}")
    if not args.dry_run:
        s3.put_object(
            Bucket=args.bucket,
            Key=MANIFEST_KEY,
            Body=text.encode("utf-8"),
            ContentType="application/json",
        )
        print(f"manifest uploaded to s3://{args.bucket}/{MANIFEST_KEY}")

    missing = [b for b, o in RULES if (b, o) not in by_key]
    if missing:
        print(
            f"\nnot provisioned: {', '.join(sorted(set(missing)))}. Those legs "
            f"will report the backend unavailable, which is a column on the "
            f"page rather than a failed run."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
