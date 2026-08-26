#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Three small VXI-11 gaps, each about telling the caller the truth.

- viFlush raises a bare NotImplementedError. An operation a backend does not
  implement owes the caller VI_ERROR_NSUP_OPER; a Python exception crossing the
  VISA boundary cannot be caught by anyone who had no reason to expect it.
- VI_ATTR_IO_PROT reads back over HiSLIP and answers VI_ERROR_NSUP_ATTR over
  VXI-11, though VPP-4.3 defines it for INSTR resources generally.
- viAssertTrigger accepts a non-default trigger protocol and returns
  VI_SUCCESS, though VXI-11 device_trigger (B.6.9) has no protocol selector, so
  nothing but the default can have been performed. HiSLIP refuses it correctly.

The last is the worst shape of the three: the caller is told the thing it asked
for happened, when what happened was something else.
"""

from __future__ import annotations

from _repro import mock_server, parse, target, verdict

from pyvisa import constants
from pyvisa.constants import ResourceAttribute as RA
from pyvisa.constants import StatusCode

from testgear import visa


def main() -> int:
    args = parse(__doc__.splitlines()[0])
    backend = target(args)
    problems = []

    with mock_server() as srv:
        for protocol, resource in (
            ("hislip", srv.hislip_resource),
            ("vxi11", srv.vxi11_resource),
        ):
            print(f"\n  {protocol}:")
            with visa.session(backend, resource, timeout=3000) as inst:
                lib, sess = inst.visalib, inst.session

                st = visa.status(
                    lib.flush, sess, constants.BufferOperation.discard_read_buffer
                )
                ok = st in (
                    StatusCode.success,
                    StatusCode.error_nonsupported_operation,
                )
                print(f"    viFlush                     -> {st!r}")
                if not ok:
                    problems.append(f"{protocol}: viFlush {st!r}")

                _, st = visa.call(lib.get_attribute, sess, RA.io_prot)
                print(f"    VI_ATTR_IO_PROT             -> {st!r}")
                if st != StatusCode.success:
                    problems.append(f"{protocol}: VI_ATTR_IO_PROT {st!r}")

                st = visa.status(
                    lib.assert_trigger, sess, constants.TriggerProtocol.on
                )
                print(f"    viAssertTrigger(non-default)-> {st!r}")
                if st != StatusCode.error_nonsupported_operation:
                    problems.append(f"{protocol}: non-default trigger {st!r}")

    for problem in problems:
        print(f"\n  {problem}")
    return verdict(bool(problems), "three VXI-11 operations misreport themselves")


if __name__ == "__main__":
    raise SystemExit(main())
