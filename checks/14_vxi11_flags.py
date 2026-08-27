#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VXI-11 B.5.3 and B.5.4: the flags and timeouts a client puts on the wire.

These are obligations on the *client*, and none of them is visible through the
VISA API. Whether `VI_ATTR_TERMCHAR_EN` actually became the `termchrset` bit, or
the session timeout actually became `io_timeout`, can only be seen by reading
the RPC the client built -- which the proxy now records.

Most of these pass. That is the point of writing them: a check that passes
today is what makes a regression tomorrow visible, and the wire format is
exactly the layer where a refactor breaks something no API-level test would
notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

# VXI-11 B.6: procedure numbers.
DEVICE_WRITE, DEVICE_READ = 11, 12
# VXI-11 Figure B.16: the operation flags.
FLAG_WAITLOCK, FLAG_END, FLAG_TERMCHRSET = 0x01, 0x08, 0x80
#: Everything Figure B.16 leaves reserved.
RESERVED_BITS = ~(FLAG_WAITLOCK | FLAG_END | FLAG_TERMCHRSET) & 0xFFFFFFFF

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def server():
    if CTX.get("server") is None:
        raise Skip("needs the mock server: these are wire-level requirements")
    return CTX["server"]


def calls(srv, proc: int) -> list[dict]:
    return [c for c in srv.vxi11_calls() if c["proc"] == proc]


@check("device_write sets the END flag on a terminated write",
       rule="VXI-11 B.6.14")
def check_write_end_flag():
    """B.6.14: with the end flag set, an END indicator accompanies the last
    byte. A client that never sets it leaves the instrument waiting for a
    message that, as far as the bus is concerned, has not finished."""
    srv = server()
    with open_inst() as inst:
        inst.set_visa_attribute(RA.send_end_enabled, True)
        srv.reset()
        inst.write("*CLS")
        writes = calls(srv, DEVICE_WRITE)
        assert writes, "no device_write reached the server"
        assert writes[-1]["flags"] & FLAG_END, (
            f"the write went out with flags {writes[-1]['flags']:#06x}, "
            f"without the END bit ({FLAG_END:#04x})"
        )
        return f"flags={writes[-1]['flags']:#06x}"


@check("clearing VI_ATTR_SEND_END_EN clears the END flag", rule="VXI-11 B.5.3")
def check_write_end_flag_cleared():
    """The other half: the attribute has to reach the wire, not just be stored.

    An implementation that keeps the attribute and never consults it looks
    correct from the API -- the value reads back -- and is wrong where it
    counts.
    """
    srv = server()
    with open_inst() as inst:
        inst.set_visa_attribute(RA.send_end_enabled, False)
        try:
            srv.reset()
            inst.visalib.write(inst.session, b"*CLS")
            writes = calls(srv, DEVICE_WRITE)
            assert writes, "no device_write reached the server"
            assert not writes[-1]["flags"] & FLAG_END, (
                f"VI_ATTR_SEND_END_EN was false but the write still carried "
                f"the END bit (flags {writes[-1]['flags']:#06x})"
            )
            return f"flags={writes[-1]['flags']:#06x}"
        finally:
            inst.set_visa_attribute(RA.send_end_enabled, True)
            inst.visalib.write(inst.session, b"\n")


@check("VI_ATTR_TERMCHAR_EN becomes termchrset, carrying the character",
       rule="VXI-11 B.5.3")
def check_read_termchrset():
    """B.5.3: termchrset is set when a termination character is specified on a
    read, and the character itself travels in termChar.

    Both halves matter. The flag without the character, or the character
    without the flag, is a read that terminates on the wrong thing.
    """
    srv = server()
    with open_inst() as inst:
        inst.set_visa_attribute(RA.termchar, ord("\n"))
        inst.set_visa_attribute(RA.termchar_enabled, True)
        try:
            srv.reset()
            inst.query("*IDN?")
            reads = calls(srv, DEVICE_READ)
            assert reads, "no device_read reached the server"
            last = reads[-1]
            assert last["flags"] & FLAG_TERMCHRSET, (
                f"termchar was enabled but the read went out with flags "
                f"{last['flags']:#06x}, without termchrset ({FLAG_TERMCHRSET:#04x})"
            )
            assert last["term_char"] == ord("\n"), (
                f"termchrset was set but termChar carried "
                f"{last['term_char']!r}, not {ord(chr(10))}"
            )
            return f"flags={last['flags']:#06x} termChar={last['term_char']}"
        finally:
            inst.set_visa_attribute(RA.termchar_enabled, False)


@check("termchrset is clear when no termination character is set",
       rule="VXI-11 B.5.3")
def check_read_termchrset_cleared():
    srv = server()
    with open_inst() as inst:
        inst.set_visa_attribute(RA.termchar_enabled, False)
        srv.reset()
        inst.query("*IDN?")
        reads = calls(srv, DEVICE_READ)
        assert reads, "no device_read reached the server"
        assert not reads[-1]["flags"] & FLAG_TERMCHRSET, (
            f"termchar was disabled but the read still carried termchrset "
            f"(flags {reads[-1]['flags']:#06x})"
        )
        return f"flags={reads[-1]['flags']:#06x}"


@check("reserved flag bits are sent as zero", rule="VXI-11 B.5.3")
def check_reserved_bits_zero():
    """B.5.3: "Controllers send undefined bits as zero (0)."

    Explicit, and the reason is forward compatibility: a later revision that
    assigns one of those bits meets a controller that has been setting it by
    accident, and the resulting behaviour is nobody's intention.
    """
    srv = server()
    with open_inst() as inst:
        srv.reset()
        inst.query("*IDN?")
        seen = srv.vxi11_calls()
        assert seen, "no device_write or device_read reached the server"
        dirty = [c for c in seen if c["flags"] & RESERVED_BITS]
        assert not dirty, (
            f"{len(dirty)} call(s) set reserved flag bits, e.g. "
            f"{dirty[0]['flags']:#010x}; B.5.3 requires undefined bits to be "
            f"sent as zero"
        )
        return f"{len(seen)} calls, all reserved bits clear"


@check("the session timeout is what reaches io_timeout", rule="VXI-11 B.5.4")
def check_io_timeout_matches():
    """B.5.4 makes io_timeout the client's statement of how long the server
    may take. If the session's VI_ATTR_TMO_VALUE does not reach it, the
    instrument is working to a deadline the caller never chose.
    """
    srv = server()
    wanted = 4321
    with open_inst() as inst:
        # Set it on the session rather than at open: open_inst already supplies
        # the suite-wide timeout, and passing it twice is a TypeError.
        inst.timeout = wanted
        srv.reset()
        inst.query("*IDN?")
        seen = srv.vxi11_calls()
        assert seen, "no call reached the server"
        mismatched = [c for c in seen if c["io_timeout"] != wanted]
        assert not mismatched, (
            f"the session timeout was {wanted}ms but "
            f"{len(mismatched)} call(s) carried io_timeout "
            f"{mismatched[0]['io_timeout']}ms"
        )
        return f"io_timeout={wanted}ms on {len(seen)} calls"


@check("a read asks for no more than the chunk size", rule="VXI-11 B.6.22")
def check_request_size():
    """B.6.22 has the server return at most requestSize bytes.

    The client's side of that bargain is asking for an amount it can actually
    take: a requestSize larger than the buffer it will read into is how a
    reply gets truncated with no error anywhere.
    """
    srv = server()
    with open_inst() as inst:
        srv.reset()
        inst.query("*IDN?")
        reads = calls(srv, DEVICE_READ)
        assert reads, "no device_read reached the server"
        sizes = {c["request_size"] for c in reads}
        assert all(s and s > 0 for s in sizes), (
            f"a read asked for {sizes}, which cannot return anything"
        )
        assert all(s <= inst.chunk_size for s in sizes), (
            f"a read asked for {max(sizes)} bytes with a chunk size of "
            f"{inst.chunk_size}; the surplus has nowhere to go"
        )
        return f"requestSize={sorted(sizes)} chunk_size={inst.chunk_size}"


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0], protocol="vxi11")
    args = parser.parse_args()
    if args.protocol != "vxi11":
        print("this suite is VXI-11 only", file=sys.stderr)
        return 4

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend,
            resource=resource,
            server=srv,
            timeout=args.timeout,
            protocol=args.protocol,
        )
        stats = harness.Stats(
            "vxi11 operation flags",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol="vxi11")
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
