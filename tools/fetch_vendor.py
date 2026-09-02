#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch one vendor's installer from the private store.

    tools/fetch_vendor.py --backend ni --os linux --bucket "$VENDOR_BUCKET" \\
        --into vendor/

NI, R&S, Keysight and TekVISA are all behind click-throughs and none of them
may be redistributed, so they cannot live in this repository -- it is public --
and they cannot be fetched automatically from the vendor either. They live in
an S3 bucket the maintainer owns, reachable by a role that can read exactly two
prefixes and write nothing.

What to download, and how to install it, comes from a manifest in the bucket
rather than from this file. Driver versions change and silent-install flags are
discovered by experiment; both should be an `aws s3 cp` away from being fixed,
not a pull request.

A checksum mismatch is fatal. A run that measures a library it cannot identify
is not a measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_KEY = "manifest.json"


def client():
    try:
        import boto3
    except ImportError:  # pragma: no cover
        print(
            "boto3 is not installed. pip install '.[ci]', or add it to the "
            "workflow's setup step.",
            file=sys.stderr,
        )
        raise SystemExit(4)
    return boto3.client("s3")


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def find(manifest: dict, backend: str, os_name: str) -> dict | None:
    for entry in manifest.get("artifacts", []):
        if entry.get("backend") == backend and entry.get("os") == os_name:
            return entry
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", required=True)
    parser.add_argument("--os", dest="os_name", required=True,
                        choices=("linux", "windows"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--into", default="vendor")
    parser.add_argument(
        "--print-install",
        metavar="PATH",
        help="write the manifest's install block here, for the caller to use",
    )
    args = parser.parse_args()

    s3 = client()
    manifest = json.loads(
        s3.get_object(Bucket=args.bucket, Key=MANIFEST_KEY)["Body"]
        .read()
        .decode("utf-8")
    )

    entry = find(manifest, args.backend, args.os_name)
    if entry is None:
        # Not an error. A backend nobody has provisioned yet should be a column
        # on the page saying so, not a red job that stops the other five.
        print(
            f"no {args.backend} installer for {args.os_name} in the manifest. "
            f"The leg will report the backend as unavailable.",
            file=sys.stderr,
        )
        return 3

    dest_dir = Path(args.into) / args.backend
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(entry["key"]).name

    print(f"{entry['key']} -> {dest}")
    s3.download_file(args.bucket, entry["key"], str(dest))

    want = entry.get("sha256", "")
    got = digest(dest)
    if want and got != want:
        print(
            f"checksum mismatch for {entry['key']}\n"
            f"  manifest: {want}\n"
            f"  download: {got}\n"
            f"Refusing to install it. A run that measures a library it cannot "
            f"identify is not a measurement.",
            file=sys.stderr,
        )
        dest.unlink(missing_ok=True)
        return 1
    if not want:
        print("WARNING: no sha256 in the manifest for this entry")

    print(f"ok: {entry.get('vendor_version', args.backend)} ({got[:12]}...)")
    if args.print_install:
        Path(args.print_install).write_text(
            json.dumps(entry.get("install", {}), indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
