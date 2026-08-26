#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stalled VXI-11 connection reports VI_ERROR_IO, about 11s late.

The proxy stops forwarding server->client bytes mid-reply and leaves the socket
open, so there is no error to notice and only a correctly-computed deadline can
end the read.

Two things are wrong: the error is VI_ERROR_IO where VPP-4.3 3.2.2 says
timeout, and there is a fixed floor of roughly 11s that belongs to no
configured value. The client's own timeout is honoured on top of that floor,
which is the signature of two deadlines in series rather than one applied to
the operation. Sweeping the timeout shows the floor directly.
"""

from __future__ import annotations

import time

from _repro import mock_server, parse, target, verdict

from pyvisa import constants, errors

from testgear import visa

TIMEOUTS_MS = (1000, 3000)
#: Anything beyond the client's own timeout by this much is the floor.
TOLERATED_OVERSHOOT = 3.0


def main() -> int:
    args = parse(__doc__.splitlines()[0])
    backend = target(args)
    print()

    wrong_error = False
    overshoots = []

    with mock_server() as srv:
        for timeout_ms in TIMEOUTS_MS:
            with visa.session(backend, srv.vxi11_resource, timeout=timeout_ms) as inst:
                srv.big_reply(8192)
                started = time.time()
                raised: Exception | None = None
                with srv.faults(stall_after_bytes=64):
                    try:
                        inst.query("TEST:BIG?")
                    except Exception as exc:  # noqa: BLE001
                        # Bound out here on purpose: Python clears the `except`
                        # name on the way out of the block, so reading it below
                        # would be an UnboundLocalError.
                        raised = exc
                    elapsed = time.time() - started

                if raised is None:
                    print(f"  timeout={timeout_ms}ms -> returned (no error)")
                    continue

                is_timeout = (
                    isinstance(raised, errors.VisaIOError)
                    and raised.error_code == constants.StatusCode.error_timeout
                )
                wrong_error = wrong_error or not is_timeout
                overshoot = elapsed - timeout_ms / 1000.0
                overshoots.append(overshoot)
                print(
                    f"  timeout={timeout_ms}ms -> {visa.visa_status(raised)} after "
                    f"{elapsed:.2f}s ({overshoot:+.2f}s beyond the timeout)"
                )

    floor = min(overshoots) if overshoots else 0.0
    print(f"\n  fixed floor beyond the configured timeout: {floor:.2f}s")
    if wrong_error:
        print("  error code is not VI_ERROR_TSK_TIMEOUT (VPP-4.3 3.2.2)")

    return verdict(
        wrong_error or floor > TOLERATED_OVERSHOOT,
        "a stalled connection is misreported and late",
    )


if __name__ == "__main__":
    raise SystemExit(main())
