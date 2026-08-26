#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A maxRecvSize of zero wedges a VXI-11 session forever.

create_link reports the largest write the server will accept (VXI-11 B.6.3,
which requires at least 1024). Answer zero and pyvisa-py's write path divides
the message into chunks of that size and never terminates: not slow, no
timeout, viWrite simply never returns.

The write runs on a daemon thread here, because the whole point is that it
cannot be interrupted -- this script has to outlive it to report anything.
"""

from __future__ import annotations

import threading
import time

from _repro import mock_server, parse, target, verdict

from testgear import visa

WEDGE_SECONDS = 10.0


def main() -> int:
    args = parse(__doc__.splitlines()[0])
    backend = target(args)

    with mock_server() as srv:
        srv.set_vxi11_faults(max_recv_size=0)
        print(f"\n  create_link will report maxRecvSize=0 on {srv.vxi11_resource}")

        outcome: dict = {}

        def attempt() -> None:
            try:
                with visa.session(backend, srv.vxi11_resource, timeout=2000) as inst:
                    outcome["reply"] = inst.query("*IDN?").strip()
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = visa.visa_status(exc)

        worker = threading.Thread(target=attempt, daemon=True)
        started = time.time()
        worker.start()
        worker.join(WEDGE_SECONDS)
        elapsed = time.time() - started

        if worker.is_alive():
            print(
                f"  the session was still inside viWrite after {elapsed:.1f}s, "
                f"with a 2000 ms timeout set"
            )
            return verdict(True, "maxRecvSize=0 wedges the session")

        if "error" in outcome:
            print(f"  refused cleanly after {elapsed:.2f}s: {outcome['error']}")
        else:
            print(f"  survived after {elapsed:.2f}s: {outcome.get('reply')!r}")
        return verdict(False, "maxRecvSize=0 wedges the session")


if __name__ == "__main__":
    raise SystemExit(main())
