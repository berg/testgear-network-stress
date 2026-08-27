#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 5.1.72 and 6.1: the operations a TCPIP INSTR session must have,
and the statuses viRead is forbidden to return.

The negative rules in 6.1 are the interesting half. Most of the read-status
rules say what viRead SHALL return; 6.1.4 and 6.1.5 say what it SHALL NOT, and
a prohibition is much easier to violate by accident -- an implementation that
computes the right status for the common case can still emit a forbidden one
when an attribute changes the rules underneath it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

#: RULE 5.1.72, restricted to the operations pyvisa exposes on the library
#: object. The formatted-I/O family (viPrintf, viScanf and friends) is
#: implemented in pyvisa itself rather than in a backend, so asking a backend
#: for it would be asking the wrong layer.
REQUIRED_OPERATIONS = (
    "read",
    "write",
    "assert_trigger",
    "read_stb",
    "clear",
    "flush",
)

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def server():
    if CTX.get("server") is None:
        raise Skip("needs the mock server")
    return CTX["server"]


@check("every operation RULE 5.1.72 requires is present and answers",
       rule="VPP-4.3 5.1.72")
def check_required_operations():
    """5.1.72 lists the operations a TCPIP INSTR resource SHALL support.

    "Support" is taken here as: the operation exists and returns a VISA
    status. An operation that raises a Python exception out of the library is
    not supported in any sense a caller can use -- it cannot be caught by
    someone who had no reason to expect it.
    """
    with open_inst() as inst:
        lib = inst.visalib
        missing = [name for name in REQUIRED_OPERATIONS if not hasattr(lib, name)]
        assert not missing, (
            f"the backend has no {', '.join(missing)}; 5.1.72 requires "
            f"{len(REQUIRED_OPERATIONS)} operations of a TCPIP INSTR resource"
        )
        return f"all {len(REQUIRED_OPERATIONS)} present"


@check("no required operation raises instead of returning a status",
       rule="VPP-4.3 5.1.72")
def check_operations_return_status():
    """The half that matters more than presence.

    viFlush is the known case: it raises NotImplementedError out of a VXI-11
    session rather than answering VI_ERROR_NSUP_OPER (docs/findings.md). This
    check states the general rule that failure covers.
    """
    from pyvisa import constants

    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        raised = []
        probes = (
            ("read_stb", lambda: visa.call(lib.read_stb, sess)),
            ("clear", lambda: visa.call(lib.clear, sess)),
            (
                "flush",
                lambda: visa.call(
                    lib.flush, sess, constants.BufferOperation.discard_read_buffer
                ),
            ),
            (
                "assert_trigger",
                lambda: visa.call(
                    lib.assert_trigger, sess, constants.TriggerProtocol.default
                ),
            ),
        )
        for name, call in probes:
            _, st = call()
            if st == visa.NOT_IMPLEMENTED:
                raised.append(name)
        assert not raised, (
            f"{', '.join(raised)} raised a Python exception rather than "
            f"returning a VISA status. An unimplemented operation owes the "
            f"caller VI_ERROR_NSUP_OPER; an exception crossing the VISA "
            f"boundary cannot be caught by anyone who had no reason to expect it"
        )
        visa.drain_errors(inst)


@check("viRead does not report VI_SUCCESS while SUPPRESS_END_EN is set",
       rule="VPP-4.3 6.1.4")
def check_suppress_end_forbids_success():
    """6.1.4 is a prohibition: with END suppressed, plain VI_SUCCESS is not an
    available answer, because VI_SUCCESS is precisely the status that means
    "END ended this read".

    A read that still reports it is telling the caller the message was
    terminated by something the caller asked it to ignore.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, set_st = visa.call(lib.set_attribute, sess, RA.suppress_end_enabled, True)
        if set_st != StatusCode.success:
            raise Skip(
                f"VI_ATTR_SUPPRESS_END_EN is not settable here ({set_st!r}), so "
                f"6.1.4's premise cannot be established"
            )
        try:
            inst.set_visa_attribute(RA.termchar_enabled, True)
            inst.set_visa_attribute(RA.termchar, ord("\n"))
            lib.write(sess, b"*IDN?\n")
            data, st = visa.call(lib.read, sess, 4096)
            assert st != StatusCode.success, (
                f"viRead returned VI_SUCCESS with VI_ATTR_SUPPRESS_END_EN set, "
                f"having read {len(data or b'')} bytes. VI_SUCCESS means END "
                f"terminated the read, and END is exactly what was suppressed"
            )
            return f"{st!r}"
        finally:
            visa.call(lib.set_attribute, sess, RA.suppress_end_enabled, False)
            visa.call(lib.set_attribute, sess, RA.termchar_enabled, False)
            visa.drain_errors(inst)


@check("viRead does not report VI_SUCCESS_TERM_CHAR while termchar is off",
       rule="VPP-4.3 6.1.5")
def check_termchar_off_forbids_term_char_status():
    """6.1.5, the mirror image: with no termination character enabled, the
    status that means "a termination character ended this read" cannot be
    true."""
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        inst.set_visa_attribute(RA.termchar_enabled, False)
        for _ in range(3):
            lib.write(sess, b"*IDN?\n")
            data, st = visa.call(lib.read, sess, 4096)
            assert st != StatusCode.success_termination_character_read, (
                f"viRead returned VI_SUCCESS_TERM_CHAR with "
                f"VI_ATTR_TERMCHAR_EN false, having read {len(data or b'')} "
                f"bytes"
            )
        return f"{st!r}"


@check("viClear discards an uncollected response", rule="VPP-4.3 5.1.8")
def check_clear_flushes_buffers():
    """5.1.8: viClear flushes the read buffer and discards the write buffers.

    Distinct from the transport-level clear the 07 suite exercises: this is
    about the *library's* buffers, so the test is that a response the library
    has already read but not handed over does not survive the clear.
    """
    srv = server()
    srv.respond("TEST:LINES?", "stale-marker")
    with open_inst() as inst:
        idn = inst.query("*IDN?").strip()
        # Leave a reply uncollected, then clear.
        inst.visalib.write(inst.session, b"TEST:LINES?\n")
        inst.clear()
        after = inst.query("*IDN?").strip()
        assert after == idn, (
            f"after viClear the next query returned {after!r} rather than "
            f"{idn!r}; the uncollected response survived the clear"
        )


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend,
            resource=resource,
            server=srv,
            timeout=args.timeout,
            protocol=args.protocol,
        )
        stats = harness.Stats(
            f"operations ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol=args.protocol)
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
