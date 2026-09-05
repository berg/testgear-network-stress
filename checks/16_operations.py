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

from testgear import script, visa  # noqa: E402
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
        return (
            f"{len(probes)} operations probed "
            f"({', '.join(name for name, _ in probes)}), all returned a status"
        )


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
        return f"the query after the clear returned {after!r}"


@check("the four REN modes RULE 6.5.6 requires of TCPIP are supported",
       rule="VPP-4.3 6.5.6")
def check_required_ren_modes():
    """6.5.6 names them exactly: DEASSERT_GTL, ASSERT_ADDRESS,
    ASSERT_ADDRESS_LLO and ADDRESS_GTL.

    This settles a question this suite previously answered by reasoning. The
    unaddressed modes had been ruled out because VXI-11 carries no RPC for
    driving REN on its own (B.6.13, B.6.14) -- correct, as it turned out, but
    an inference. 6.5.6 lists the required four, and they are exactly the
    addressed ones, so refusing the rest is conforming by citation rather than
    by argument.
    """
    from pyvisa import constants

    required = (
        ("VI_GPIB_REN_DEASSERT_GTL", constants.RENLineOperation.deassert_gtl),
        ("VI_GPIB_REN_ASSERT_ADDRESS", constants.RENLineOperation.asrt_address),
        (
            "VI_GPIB_REN_ASSERT_ADDRESS_LLO",
            constants.RENLineOperation.asrt_address_llo,
        ),
        ("VI_GPIB_REN_ADDRESS_GTL", constants.RENLineOperation.address_gtl),
    )
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        refused = []
        for name, mode in required:
            st = visa.status(lib.gpib_control_ren, sess, mode)
            if st != StatusCode.success:
                refused.append(f"{name} ({st!r})")
        # Leave the instrument addressed and in remote, as the other suites
        # expect to find it.
        visa.status(
            lib.gpib_control_ren, sess, constants.RENLineOperation.asrt_address
        )
        assert not refused, (
            f"{len(refused)} of the four modes 6.5.6 requires were refused: "
            f"{', '.join(refused)}"
        )
        return f"all four supported"


@check("viFlush on an empty buffer does nothing rather than failing",
       rule="VPP-4.3 6.2.5")
def check_flush_empty_buffer():
    """6.2.5: flushing an empty buffer performs no action on it.

    "No action" has to include "no error": flushing defensively, without
    knowing whether anything is buffered, is the ordinary way to call this.
    """
    from pyvisa import constants

    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        first = visa.status(
            lib.flush, sess, constants.BufferOperation.discard_read_buffer
        )
        if first == visa.NOT_IMPLEMENTED:
            raise Skip("viFlush is not implemented on this transport")
        second = visa.status(
            lib.flush, sess, constants.BufferOperation.discard_read_buffer
        )
        assert second == first, (
            f"flushing an already-empty buffer returned {second!r} where the "
            f"first flush returned {first!r}"
        )
        assert inst.query("*IDN?").strip(), "the session broke after two flushes"
        return f"{second!r}"


@check("VI_ATTR_USER_DATA agrees with its width-specific twin",
       rule="VPP-4.3 3.2.8")
def check_user_data_consistency():
    """3.2.7 and 3.2.8: on a 32-bit framework VI_ATTR_USER_DATA equals
    VI_ATTR_USER_DATA_32, and on a 64-bit one it equals VI_ATTR_USER_DATA_64.

    Two names for one storage slot. A caller that writes through one and reads
    through the other -- which is exactly what portable code does -- gets its
    own value back or does not.
    """
    import struct

    from pyvisa.constants import ResourceAttribute as RA

    sixty_four = struct.calcsize("P") == 8
    twin_name = "VI_ATTR_USER_DATA_64" if sixty_four else "VI_ATTR_USER_DATA_32"
    twin = getattr(RA, "user_data_64" if sixty_four else "user_data_32", None)
    if twin is None:
        # Not a backend gap. pyvisa's ResourceAttribute enum has only
        # VI_ATTR_USER_DATA, so the width-specific names cannot be reached
        # through this API at all and no backend could satisfy the rule.
        raise Skip(
            f"pyvisa exposes no {twin_name}; 3.2.7/3.2.8 cannot be checked "
            f"through this API, and the gap is pyvisa's rather than a "
            f"backend's"
        )

    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, st = visa.call(lib.set_attribute, sess, RA.user_data, 0x5A5A)
        if st != StatusCode.success:
            raise Skip(f"VI_ATTR_USER_DATA is not writeable here ({st!r})")
        plain, plain_st = visa.call(lib.get_attribute, sess, RA.user_data)
        sized, sized_st = visa.call(lib.get_attribute, sess, twin)
        assert sized_st == StatusCode.success, (
            f"{twin_name} is not readable ({sized_st!r}) on a "
            f"{'64' if sixty_four else '32'}-bit framework"
        )
        assert plain_st == StatusCode.success and plain == sized, (
            f"VI_ATTR_USER_DATA reads {plain!r} but {twin_name} reads "
            f"{sized!r}; they name one storage slot"
        )
        return f"{plain!r} through both names"


if __name__ == "__main__":
    script.run()
