#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 section 5.1: the attributes an INSTR session is required to have.

Four rules, each a plain list of attributes a conforming implementation SHALL
support. That makes them the most mechanical checks in the suite and among the
most useful: an attribute nobody happens to read is an attribute that can go
missing without anything failing, until the day a caller reads it.

The rules stack by specificity -- every INSTR session, then every
message-based one, then TCPIP, then HiSLIP specifically -- so each check
reports which list it is enforcing and skips the ones that do not apply to the
transport under test.

An attribute is "supported" here if reading it returns a status other than
VI_ERROR_NSUP_ATTR. Whether the *value* is right is a different question and
mostly a different rule; this is about presence.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

#: RULE 5.1.11 -- every INSTR resource, whatever the interface.
ALL_INSTR = (
    ("VI_ATTR_INTF_TYPE", RA.interface_type),
    ("VI_ATTR_INTF_INST_NAME", RA.interface_instrument_name),
    ("VI_ATTR_TMO_VALUE", RA.timeout_value),
    ("VI_ATTR_INTF_NUM", RA.interface_number),
    ("VI_ATTR_DMA_ALLOW_EN", RA.dma_allow_enabled),
)

#: 5.1.11 lists VI_ATTR_TRIG_ID among the attributes every INSTR resource
#: SHALL support, and **no** implementation supports it on TCPIP -- pyvisa-py,
#: NI-VISA and R&S VISA all answer VI_ERROR_NSUP_ATTR.
#:
#: Three independent implementations agreeing is not three bugs. The attribute
#: selects a hardware trigger line, which a TCPIP session does not have, so the
#: rule reads as drafted for the backplane interfaces and applied to INSTR
#: generally. Reported rather than failed: a check nobody can pass tells you
#: about the spec, not the implementations.
UNIVERSALLY_ABSENT = (("VI_ATTR_TRIG_ID", RA.trigger_id),)

#: RULE 5.1.12 -- message-based interfaces, TCPIP among them.
MESSAGE_BASED = (
    ("VI_ATTR_IO_PROT", RA.io_prot),
    ("VI_ATTR_SEND_END_EN", RA.send_end_enabled),
    ("VI_ATTR_SUPPRESS_END_EN", RA.suppress_end_enabled),
    ("VI_ATTR_TERMCHAR", RA.termchar),
    ("VI_ATTR_TERMCHAR_EN", RA.termchar_enabled),
    ("VI_ATTR_RD_BUF_OPER_MODE", RA.read_buffer_operation_mode),
    ("VI_ATTR_WR_BUF_OPER_MODE", RA.write_buffer_operation_mode),
    ("VI_ATTR_FILE_APPEND_EN", RA.file_append_enabled),
)

#: RULE 5.1.16 -- any TCPIP INSTR resource.
TCPIP_INSTR = (
    ("VI_ATTR_TCPIP_ADDR", RA.tcpip_address),
    ("VI_ATTR_TCPIP_HOSTNAME", RA.tcpip_hostname),
    ("VI_ATTR_TCPIP_IS_HISLIP", RA.tcpip_is_hislip),
    ("VI_ATTR_TCPIP_DEVICE_NAME", RA.tcpip_device_name),
)

#: RULE 5.1.17 -- HiSLIP specifically.
HISLIP_INSTR = (
    ("VI_ATTR_TCPIP_PORT", RA.tcpip_port),
    ("VI_ATTR_TCPIP_NODELAY", RA.tcpip_nodelay),
    ("VI_ATTR_TCPIP_KEEPALIVE", RA.tcpip_keepalive),
    ("VI_ATTR_TCPIP_HISLIP_OVERLAP_EN", RA.tcpip_hislip_overlap_enable),
    ("VI_ATTR_TCPIP_HISLIP_VERSION", RA.tcpip_hislip_version),
    ("VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB", RA.tcpip_hislip_max_message_kb),
)

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def missing(inst, required) -> list[str]:
    """Which of `required` this session does not support."""
    absent = []
    for name, attribute in required:
        _, st = visa.call(inst.visalib.get_attribute, inst.session, attribute)
        if st in (StatusCode.error_nonsupported_attribute, visa.NOT_IMPLEMENTED):
            absent.append(name)
    return absent


@check("VI_ATTR_TRIG_ID is absent everywhere, as RULE 5.1.11 does not anticipate",
       rule="VPP-4.3 5.1.11")
def check_trig_id_universally_absent():
    """A rule no implementation satisfies, recorded rather than prosecuted.

    5.1.11 requires VI_ATTR_TRIG_ID of every INSTR resource. pyvisa-py,
    NI-VISA and R&S VISA all answer VI_ERROR_NSUP_ATTR on a TCPIP session. The
    attribute selects a hardware trigger line, which TCPIP does not have.

    Three independent implementations agreeing is evidence about the clause,
    not about the implementations, so this passes when the attribute is absent
    and would want investigating if it ever appeared.
    """
    with open_inst() as inst:
        absent = missing(inst, UNIVERSALLY_ABSENT)
        if not absent:
            return (
                "VI_ATTR_TRIG_ID is supported here, which no implementation "
                "did when this was written -- worth a look"
            )
        return "absent, as in every implementation measured"


@check("every other attribute RULE 5.1.11 requires of an INSTR session is present",
       rule="VPP-4.3 5.1.11")
def check_all_instr_attributes():
    with open_inst() as inst:
        absent = missing(inst, ALL_INSTR)
        assert not absent, (
            f"{len(absent)} of {len(ALL_INSTR)} required attributes are "
            f"unsupported: {', '.join(absent)}"
        )
        return f"all {len(ALL_INSTR)} present"


@check("every attribute RULE 5.1.12 requires of a message-based session is present",
       rule="VPP-4.3 5.1.12")
def check_message_based_attributes():
    """5.1.12 names TCPIP explicitly, so this applies to both transports."""
    with open_inst() as inst:
        absent = missing(inst, MESSAGE_BASED)
        assert not absent, (
            f"{len(absent)} of {len(MESSAGE_BASED)} required attributes are "
            f"unsupported: {', '.join(absent)}"
        )
        return f"all {len(MESSAGE_BASED)} present"


@check("every attribute RULE 5.1.16 requires of a TCPIP INSTR session is present",
       rule="VPP-4.3 5.1.16")
def check_tcpip_attributes():
    with open_inst() as inst:
        absent = missing(inst, TCPIP_INSTR)
        assert not absent, (
            f"{len(absent)} of {len(TCPIP_INSTR)} required attributes are "
            f"unsupported: {', '.join(absent)}"
        )
        return f"all {len(TCPIP_INSTR)} present"


@check("every attribute RULE 5.1.17 requires of a HiSLIP session is present",
       rule="VPP-4.3 5.1.17", protocols=("hislip",))
def check_hislip_attributes():
    """5.1.17 binds "a HiSLIP TCPIP system", so it does not apply to VXI-11.

    VI_ATTR_TCPIP_PORT is the interesting member: it has been observed set but
    unreadable on HiSLIP sessions before, and this rule is the reason that
    matters -- it is required, not optional.
    """
    with open_inst() as inst:
        absent = missing(inst, HISLIP_INSTR)
        assert not absent, (
            f"{len(absent)} of {len(HISLIP_INSTR)} required attributes are "
            f"unsupported: {', '.join(absent)}"
        )
        return f"all {len(HISLIP_INSTR)} present"


@check("VI_ATTR_TCPIP_IS_HISLIP tells the truth about the transport",
       rule="VPP-4.3 5.1.29")
def check_is_hislip_correct():
    """5.1.29 and 5.1.30 tie the protocol to the device name: `inst`/`gpib`
    means VXI-11, `hislip` means HiSLIP.

    So VI_ATTR_TCPIP_IS_HISLIP is not a free-floating flag -- it has a right
    answer, derivable from the resource string, and a caller choosing
    behaviour on it is entitled to that answer.
    """
    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.tcpip_is_hislip
        )
        assert st == StatusCode.success, (
            f"VI_ATTR_TCPIP_IS_HISLIP is not readable ({st!r}); 5.1.16 "
            f"requires it on every TCPIP INSTR session"
        )
        expected = CTX["protocol"] == "hislip"
        assert bool(value) == expected, (
            f"the resource is {CTX['resource']!r}, so 5.1.29/5.1.30 make this "
            f"{'HiSLIP' if expected else 'VXI-11'}, but "
            f"VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"
        )
        return (
            f"the resource is {CTX['resource']!r}, so this is "
            f"{'HiSLIP' if expected else 'VXI-11'}; "
            f"VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"
        )


@check("the service request event is supported", rule="VPP-4.3 5.1.54")
def check_srq_event_supported():
    """5.1.54: a TCPIP INSTR resource SHALL support generating
    VI_EVENT_SERVICE_REQ.

    Enabling it is the whole check -- whether one ever arrives depends on the
    instrument, and the SRQ suites cover that.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        st = visa.status(lib.enable_event, sess, visa.SRQ, visa.QUEUE)
        if st == StatusCode.success:
            visa.status(lib.disable_event, sess, visa.SRQ, visa.QUEUE)
        assert st == StatusCode.success, (
            f"enabling VI_EVENT_SERVICE_REQ returned {st!r}"
        )
        return f"viEnableEvent(VI_EVENT_SERVICE_REQ) returned {st!r}"


@check("VI_ATTR_INTF_TYPE reports TCPIP", rule="VPP-4.3 5.1.11")
def check_interface_type():
    """The value behind 5.1.11's first attribute.

    Presence without correctness would be a thin promise: a caller reads
    VI_ATTR_INTF_TYPE precisely to branch on the interface.
    """
    from pyvisa import constants

    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.interface_type
        )
        assert st == StatusCode.success, f"VI_ATTR_INTF_TYPE is not readable ({st!r})"
        assert value == constants.InterfaceType.tcpip, (
            f"expected VI_INTF_TCPIP, got {value!r}"
        )
        return f"VI_ATTR_INTF_TYPE reads {value!r}"


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
            f"required attributes ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol=args.protocol)
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
