#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 section 4.3: how a resource string is parsed and what it selects.

The resource string is the only thing most callers ever type, and the rules
governing it are unusually precise: which protocol a device name selects, what
happens when it is omitted, and that matching is case-insensitive everywhere.

Case is the one worth having. A caller that stores a resource name in a
configuration file and gets its capitalisation from a human is relying on
4.3.17 without ever having read it, and an implementation that compares
case-sensitively works perfectly until the day somebody types `TCPIP0` as
`tcpip0`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}


def open_resource(name: str, **kwargs):
    rm = CTX["backend"].resource_manager()
    inst = rm.open_resource(name, open_timeout=5000, **kwargs)
    inst.timeout = CTX["timeout"]
    return inst


def server():
    if CTX.get("server") is None:
        raise Skip("needs the mock server, whose address is known")
    return CTX["server"]


@check("viOpen matches the resource name case-insensitively",
       rule="VPP-4.3 4.3.17")
def check_open_case_insensitive():
    """4.3.17: viOpen SHALL use a case-insensitive compare.

    Worth testing on the parts a human types rather than on the whole string:
    the interface prefix and the device name.
    """
    name = CTX["resource"]
    shouted = name.upper()
    if shouted == name:
        raise Skip("the resource name has no case to change")
    try:
        inst = open_resource(shouted)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{shouted!r} was refused ({visa.visa_status(exc)}) while "
            f"{name!r} opens; 4.3.17 requires the compare to be "
            f"case-insensitive"
        ) from None
    try:
        reply = inst.query("*IDN?")
        assert reply.strip(), "the session opened but answered nothing"
    finally:
        with __import__("contextlib").suppress(Exception):
            inst.close()
    return shouted


@check("every component of the resource name is case-insensitive",
       rule="VPP-4.3 4.3.17")
def check_open_lowercase():
    """4.3.17 admits no exceptions, so this varies the components separately
    and names the one that fails.

    Reporting *which* part is case-sensitive is the difference between a bug
    report somebody can act on and an observation that lowercase "does not
    work".
    """
    import contextlib

    name = CTX["resource"]
    head, _, rest = name.partition("::")
    body, _, suffix = rest.rpartition("::")
    variants = {
        "interface prefix": f"{head.lower()}::{body}::{suffix}",
        "device name": f"{head}::{body.lower()}::{suffix}",
        "resource class suffix": f"{head}::{body}::{suffix.lower()}",
        "the whole name": name.lower(),
    }

    refused = {}
    for label, candidate in variants.items():
        if candidate == name:
            continue
        try:
            inst = open_resource(candidate)
        except Exception as exc:  # noqa: BLE001
            refused[label] = visa.visa_status(exc)
            continue
        with contextlib.suppress(Exception):
            inst.close()

    assert not refused, (
        "4.3.17 requires a case-insensitive compare, but lowercasing "
        + ", ".join(f"the {label} was refused with {st}" for label, st in refused.items())
    )
    return f"{len(variants)} case variants all accepted"


@check("a 'hislip' device name selects HiSLIP", rule="VPP-4.3 4.3.6",
       protocols=("hislip",))
def check_hislip_name_selects_hislip():
    """4.3.6: an alphanumeric device name starting with 'hislip' means connect
    via HiSLIP. VI_ATTR_TCPIP_IS_HISLIP is where the answer shows."""
    with visa.session(CTX["backend"], CTX["resource"], timeout=CTX["timeout"]) as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.tcpip_is_hislip
        )
        assert st == StatusCode.success, (
            f"VI_ATTR_TCPIP_IS_HISLIP is not readable ({st!r})"
        )
        assert bool(value), (
            f"the device name in {CTX['resource']!r} starts with 'hislip' but "
            f"VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"
        )
        return f"VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"


@check("an 'inst' device name selects VXI-11", rule="VPP-4.3 4.3.7",
       protocols=("vxi11",))
def check_inst_name_selects_vxi11():
    """4.3.7: 'vxi' for VXI-11.1, 'gpib' for VXI-11.2, 'inst' for VXI-11.3."""
    with visa.session(CTX["backend"], CTX["resource"], timeout=CTX["timeout"]) as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.tcpip_is_hislip
        )
        assert st == StatusCode.success, (
            f"VI_ATTR_TCPIP_IS_HISLIP is not readable ({st!r})"
        )
        assert not value, (
            f"the device name in {CTX['resource']!r} starts with 'inst', which "
            f"4.3.7 makes VXI-11, but VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"
        )
        return f"VI_ATTR_TCPIP_IS_HISLIP reads {value!r}"


@check("an omitted device name connects over VXI-11", rule="VPP-4.3 4.3.8",
       protocols=("vxi11",))
def check_omitted_device_name():
    """4.3.8: with the device name omitted, an IPv4 host that supports VXI-11
    is reached over VXI-11.

    `TCPIP::host::INSTR` is the shortest resource string anyone writes, so this
    is the default path for a caller who has only been given an address.
    """
    srv = server()
    if srv.ports.get("vxi11_port") != 111 and "127.0.0.1::" not in srv.vxi11_resource:
        # The short form carries no port, so it can only work when the server
        # is reachable through the portmapper on its standard port.
        if not srv._portmap:
            raise Skip(
                "the mock is not serving a portmapper, so a resource string "
                "with no port cannot reach it"
            )
    short = f"TCPIP::{srv.host}::INSTR"
    try:
        inst = open_resource(short)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{short!r} was refused ({visa.visa_status(exc)}); 4.3.8 makes an "
            f"omitted device name a VXI-11 connection"
        ) from None
    try:
        reply = inst.query("*IDN?")
        assert reply.strip(), "the session opened but answered nothing"
    finally:
        with __import__("contextlib").suppress(Exception):
            inst.close()
    return short


@check("a dotted IPv4 address is accepted as the host", rule="VPP-4.3 4.3.4")
def check_ipv4_host():
    """4.3.4: an implementation SHALL support a hostname or a dot-delimited
    IPv4 address.

    The mock is addressed by IPv4 throughout, so reaching it at all
    demonstrates the address half; this states it explicitly so the rule is
    cited rather than assumed.
    """
    import re as _re

    name = CTX["resource"]
    assert _re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", name), (
        f"this run is not addressed by IPv4 ({name!r}), so 4.3.4's address "
        f"form is not being exercised"
    )
    with visa.session(CTX["backend"], name, timeout=CTX["timeout"]) as inst:
        assert inst.query("*IDN?").strip(), "the IPv4-addressed session answered nothing"
    return name


@check("viParseRsrc agrees with viOpen about the resource",
       rule="VPP-4.3 4.3.20")
def check_parse_matches_open():
    """4.3.20 requires viParseRsrc to match case-insensitively too, and the
    point of parsing is that it agrees with opening.

    A parser that accepts a string viOpen rejects, or normalises it
    differently, sends a caller looking for a device that its own library will
    not open.
    """
    rm = CTX["backend"].resource_manager()
    name = CTX["resource"]
    try:
        parsed = rm.resource_info(name)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"viParseRsrc refused {name!r} ({visa.visa_status(exc)}) although "
            f"viOpen accepts it"
        ) from None

    assert parsed is not None, "viParseRsrc returned nothing"
    try:
        shouted = rm.resource_info(name.upper())
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"viParseRsrc refused the upper-cased name ({visa.visa_status(exc)}); "
            f"4.3.20 requires a case-insensitive compare"
        ) from None
    assert (
        parsed.interface_type == shouted.interface_type
    ), "viParseRsrc gave different answers for the same name in different case"
    return f"{parsed.interface_type!r}"


@check("viSetBuf reports rather than raises when a size is unsupported",
       rule="VPP-4.3 6.2.3")
def check_set_buf():
    """6.2.3 and 6.2.4: a TCPIP INSTR resource that cannot set the I/O buffer
    size answers `VI_ERROR_NSUP_OPER` -- an answer, not an exception."""
    from pyvisa import constants

    with visa.session(CTX["backend"], CTX["resource"], timeout=CTX["timeout"]) as inst:
        st = visa.status(
            inst.visalib.set_buffer, inst.session, constants.BufferType.io_in, 8192
        )
        if st == visa.NOT_IMPLEMENTED:
            raise AssertionError(
                "viSetBuf raised a Python exception instead of returning a "
                "status; 6.2.3 makes VI_ERROR_NSUP_OPER the answer when the "
                "size cannot be set"
            )
        assert st in (
            StatusCode.success,
            StatusCode.error_nonsupported_operation,
        ), f"viSetBuf returned {st!r}"
        return f"{st!r}"


if __name__ == "__main__":
    script.run()
