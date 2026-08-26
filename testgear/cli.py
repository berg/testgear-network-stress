# SPDX-License-Identifier: GPL-3.0-or-later
"""The argument surface every check script shares.

One parser, because the scripts are meant to be run both individually and as a
suite, and an option that means `-t` in one script and `--timeout` in another
is an option nobody passes correctly under time pressure.

Two things it resolves that the scripts should not have to:

- **Which backend.** `--backend py` is the default; anything else is a
  comparison run. Unavailable backends fail here with a message naming what to
  install, rather than deep inside a check as an obscure pyvisa error.

- **What to point at.** With no `--resource`, scripts start their own mock
  server and use that, so the whole suite runs on a laptop with no bench. With
  `--resource`, they talk to real hardware and the mock never starts.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from . import backends


def build_parser(description: str, protocol: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument(
        "-b",
        "--backend",
        default=os.environ.get("TESTGEAR_BACKEND", "py"),
        help="VISA implementation under test: py, ni, rs, keysight, tek, sim, "
        "or a path to a VISA shared library (env: TESTGEAR_BACKEND)",
    )
    parser.add_argument(
        "-r",
        "--resource",
        default=os.environ.get("TESTGEAR_RESOURCE"),
        help="VISA resource to test against. Omit to start a mock server and "
        "use that, which needs no hardware (env: TESTGEAR_RESOURCE)",
    )
    parser.add_argument(
        "-p",
        "--protocol",
        default=protocol or os.environ.get("TESTGEAR_PROTOCOL", "hislip"),
        choices=("hislip", "vxi11"),
        help="which transport to exercise against the mock server",
    )
    parser.add_argument(
        "-n", "--iterations", type=int, default=200, help="iteration count"
    )
    parser.add_argument(
        "-t", "--timeout", type=int, default=5000, help="VISA timeout in ms"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="also write the results as JSON, for the comparison table",
    )
    parser.add_argument(
        "--big-query",
        default=os.environ.get("TESTGEAR_BIG_QUERY"),
        help="a query returning a large response, for the multi-chunk read "
        "checks. Defaults to the mock's TEST:BIG?; against real hardware, "
        "name one the instrument implements or those checks skip",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="run the mock server without its fault-injecting proxy. "
        "Transport faults stop working; for throughput measurement, where "
        "the proxy's extra copy is what would be measured",
    )
    return parser


def resolve_backend(args) -> backends.Resolved:
    """The backend named on the command line, or exit saying why not.

    Exiting here is deliberate. A missing backend is a setup problem, and
    letting the run continue produces a report whose columns silently differ
    in what they mean.
    """
    resolved = backends.resolve(args.backend)
    if not resolved.available:
        print(f"backend {args.backend!r} is not available: {resolved.reason}", file=sys.stderr)
        if resolved.spec.source:
            print(f"  get it from: {resolved.spec.source}", file=sys.stderr)
        available = ", ".join(backends.available_ids())
        print(f"  available here: {available}", file=sys.stderr)
        sys.exit(4)
    return resolved


def context(args, resolved: backends.Resolved, resource: str) -> dict:
    """The provenance block printed at the top of every run.

    A result that cannot name the tree that produced it is not reproducible,
    and this suite exists to compare trees.
    """
    info = backends.provenance(resolved)
    info["resource"] = resource
    note = backends.pyvisa_py_tree_note()
    if note:
        info["warning"] = note
    return info


@contextlib.contextmanager
def open_target(args):
    """Resolve what to test and what to test it against.

    Yields ``(resolved_backend, resource, server_or_None)``. The mock server
    is started only when no resource was named, and is always stopped again,
    so a check that raises does not leave a server holding a port.
    """
    resolved = resolve_backend(args)

    if args.resource:
        yield resolved, args.resource, None
        return

    if not resolved.spec.networked:
        print(
            f"{resolved.name} does not speak network protocols, so it cannot "
            f"talk to the mock server. Name a --resource it can reach.",
            file=sys.stderr,
        )
        sys.exit(4)

    from .server import mock_server

    with mock_server(proxy=not args.no_proxy) as server:
        yield resolved, server.resource(args.protocol), server
