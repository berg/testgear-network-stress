#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Does each fault knob actually do anything?

A check built on fault injection is only worth what the injection is worth. If
a knob is a no-op, the check around it passes for a reason that has nothing to
do with what it claims to test -- and it passes *quietly*, which is the worst
way to be wrong.

This suite has already produced two of those. One armed a HiSLIP fault against
a message type the server never sends, so the fault could not fire and the
resulting success was reported as a client failure. Another could not see any
message larger than a single read, so injection into large replies silently did
nothing.

So each knob is exercised twice here -- once armed, once not -- and the two
outcomes have to differ. A knob whose armed and unarmed runs are
indistinguishable is reported as INERT, whatever the checks built on it happen
to say.

    tools/verify_faults.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import backends, visa  # noqa: E402
from testgear.server import mock_server  # noqa: E402

DEVICE_WRITE, DEVICE_READ, CREATE_LINK = 11, 12, 10


class Outcome:
    """What one attempt produced: a value, or the error that replaced it."""

    def __init__(self, value=None, error=None, elapsed=0.0):
        self.value = value
        self.error = error
        self.elapsed = elapsed

    def __repr__(self) -> str:
        if self.error:
            return f"{self.error} in {self.elapsed:.2f}s"
        shown = self.value if isinstance(self.value, str) else repr(self.value)
        if isinstance(shown, str) and len(shown) > 42:
            shown = f"{shown[:39]}... ({len(self.value)} bytes)"
        return f"{shown} in {self.elapsed:.2f}s"

    def signature(self):
        """What has to differ between armed and unarmed for a knob to count."""
        return (self.error or "", self.value if self.error else _shape(self.value))


def _shape(value):
    if isinstance(value, str):
        return ("str", len(value))
    return value


def attempt(fn) -> Outcome:
    started = time.time()
    try:
        return Outcome(value=fn(), elapsed=time.time() - started)
    except Exception as exc:  # noqa: BLE001
        return Outcome(error=visa.visa_status(exc), elapsed=time.time() - started)


def report(name: str, armed: Outcome, control: Outcome, *, timing_only=False) -> bool:
    """Print the pair and say whether the knob demonstrably did something."""
    if timing_only:
        # For knobs whose only effect is delay, the values are supposed to
        # match; a 3x separation is the signal.
        worked = armed.elapsed > control.elapsed * 3 and armed.elapsed > 0.05
    else:
        worked = armed.signature() != control.signature()
    verdict = "WORKS " if worked else "INERT "
    print(f"  {verdict} {name}")
    print(f"           armed:   {armed}")
    print(f"           control: {control}")
    return worked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=int, default=2000)
    args = parser.parse_args()

    backend = backends.resolve("py")
    if not backend.available:
        print(f"pyvisa-py unavailable: {backend.reason}", file=sys.stderr)
        return 4

    results: dict[str, bool] = {}

    # -- transport knobs, exercised over VXI-11 --------------------------
    print("\ntransport faults (proxy, byte level)")
    with mock_server() as srv:
        res = srv.vxi11_resource

        def query(cmd="*IDN?"):
            with visa.session(backend, res, timeout=args.timeout) as inst:
                return inst.query(cmd)

        srv.big_reply(4096)
        with srv.faults(drop_after_bytes=64):
            armed = attempt(lambda: query("TEST:BIG?"))
        control = attempt(lambda: query("TEST:BIG?"))
        results["drop_after_bytes"] = report("drop_after_bytes", armed, control)

        with srv.faults(stall_after_bytes=64):
            armed = attempt(lambda: query("TEST:BIG?"))
        control = attempt(lambda: query("TEST:BIG?"))
        results["stall_after_bytes"] = report("stall_after_bytes", armed, control)

        # Dribble and latency change timing, not content.
        srv.big_reply(300)
        with srv.faults(dribble=True):
            armed = attempt(lambda: query("TEST:BIG?"))
        control = attempt(lambda: query("TEST:BIG?"))
        results["dribble"] = report("dribble", armed, control, timing_only=True)

        with srv.faults(latency_ms=250):
            armed = attempt(lambda: query("*IDN?"))
        control = attempt(lambda: query("*IDN?"))
        results["latency_ms"] = report("latency_ms", armed, control, timing_only=True)

        # -- instrument knobs -------------------------------------------
        print("\ninstrument faults (virtual device)")
        with srv.faults(read_delay_ms=400):
            armed = attempt(lambda: query("*IDN?"))
        control = attempt(lambda: query("*IDN?"))
        results["read_delay_ms"] = report(
            "read_delay_ms", armed, control, timing_only=True
        )

        with srv.faults(fail_next_write=True):
            armed = attempt(lambda: query("*IDN?"))
        control = attempt(lambda: query("*IDN?"))
        results["fail_next_write"] = report("fail_next_write", armed, control)

        def poll():
            with visa.session(backend, res, timeout=args.timeout) as inst:
                return inst.read_stb()

        with srv.faults(forced_stb=0x5A):
            armed = attempt(poll)
        control = attempt(poll)
        results["forced_stb"] = report("forced_stb", armed, control)

        # -- VXI-11 RPC knobs -------------------------------------------
        print("\nVXI-11 RPC faults (proxy, record level)")
        with srv.vxi11_faults(error_on_proc=DEVICE_READ, error_code=21):
            armed = attempt(query)
        control = attempt(query)
        results["vxi11 error_on_proc"] = report("error_on_proc", armed, control)

        with srv.vxi11_faults(stale_reply_before_proc=DEVICE_READ):
            armed = attempt(query)
        control = attempt(query)
        results["vxi11 stale_reply"] = report("stale_reply_before_proc", armed, control)

        # max_recv_size acts at create_link, so the whole session is the probe.
        def open_and_query():
            with visa.session(backend, res, timeout=args.timeout) as inst:
                return inst.query("*IDN?")

        srv.set_vxi11_faults(max_recv_size=64)
        armed_writes = None
        try:
            with visa.session(backend, res, timeout=args.timeout) as inst:
                srv.reset()
                srv.set_vxi11_faults(max_recv_size=64)
                inst.write("TEST:SILENT? " + "x" * 400)
                time.sleep(0.2)
                armed_writes = len(srv.writes())
        except Exception:  # noqa: BLE001
            pass
        srv.set_vxi11_faults()
        control_writes = None
        try:
            with visa.session(backend, res, timeout=args.timeout) as inst:
                srv.reset()
                inst.write("TEST:SILENT? " + "x" * 400)
                time.sleep(0.2)
                control_writes = len(srv.writes())
        except Exception:  # noqa: BLE001
            pass
        worked = (
            armed_writes is not None
            and control_writes is not None
            and armed_writes > control_writes
        )
        print(f"  {'WORKS ' if worked else 'INERT '} max_recv_size")
        print(f"           armed:   {armed_writes} writes reached the instrument")
        print(f"           control: {control_writes} writes")
        results["vxi11 max_recv_size"] = worked
        srv.set_vxi11_faults()

    # -- HiSLIP message knobs -------------------------------------------
    print("\nHiSLIP message faults (proxy, message level)")
    with mock_server() as srv:
        res = srv.hislip_resource

        def hquery(cmd="*IDN?"):
            with visa.session(backend, res, timeout=args.timeout) as inst:
                return inst.query(cmd)

        with srv.hislip_faults(skew_data_end_id=4):
            armed = attempt(hquery)
        control = attempt(hquery)
        results["hislip skew_data_end_id"] = report(
            "skew_data_end_id", armed, control
        )

        with srv.hislip_faults(break_prologue=True):
            armed = attempt(hquery)
        control = attempt(hquery)
        results["hislip break_prologue"] = report("break_prologue", armed, control)

        # skew_data_id needs a Data message, which this server never sends.
        srv.big_reply(400_000)
        srv.reset()
        try:
            hquery("TEST:BIG?")
        except Exception:  # noqa: BLE001
            pass
        kinds = {m["message_type"] for m in srv.hislip_messages() if m["from"] == "server"}
        reachable = 6 in kinds  # Data
        print(f"  {'WORKS ' if reachable else 'UNREACHABLE'} skew_data_id")
        print(
            "           the server never sends a Data message (only DataEND), "
            "so this knob has nothing to act on here"
            if not reachable
            else "           a Data message occurs, so the knob is reachable"
        )
        results["hislip skew_data_id"] = reachable

    inert = [k for k, ok in results.items() if not ok]
    print(f"\n{len(results) - len(inert)}/{len(results)} knobs demonstrably work")
    if inert:
        print("INERT or unreachable:")
        for k in inert:
            print(f"  - {k}")
    return 1 if inert else 0


if __name__ == "__main__":
    raise SystemExit(main())
