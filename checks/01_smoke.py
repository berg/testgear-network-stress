#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every VISA operation the session implements, once, with a capability matrix.

Run this first. It is the quickest way to see whether anything is broken
outright, and the capability matrix tells you what the rest of the suite will
be able to exercise against this target.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402


def query_rate(inst, count: int = 8) -> float:
    """Queries per second, for spotting an instrument left in local mode.

    A GPIB instrument in local mode still answers, just far more slowly, so a
    REN operation that does not stick shows up as throughput and not as an
    error. `VI_ATTR_GPIB_REN_STATE` would be the direct check, but the VISA
    spec defines it for GPIB resources, not TCPIP INSTR.
    """
    start = time.time()
    for _ in range(count):
        inst.query("*IDN?")
    return count / max(time.time() - start, 1e-6)


#: Attributes every TCPIP INSTR session should answer, whatever the transport.
COMMON_ATTRIBUTES = (
    ("tcpip_address", None),
    ("tcpip_hostname", None),
    ("tcpip_device_name", None),
    ("send_end_enabled", None),
    ("termchar", None),
    ("termchar_enabled", None),
    ("io_prot", None),
)

#: HiSLIP-only attributes. Asking for these over VXI-11 is not a failure of
#: the backend, so they are only consulted where they mean something.
HISLIP_ATTRIBUTES = (
    ("tcpip_is_hislip", True),
    ("tcpip_hislip_version", None),
    ("tcpip_hislip_max_message_kb", None),
    ("tcpip_hislip_overlap_enable", None),
)


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"smoke ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session

            idn = inst.query("*IDN?").strip()
            stats.check(bool(idn), f"*IDN? -> {idn}")
            visa.drain_errors(inst)

            # -- read/write -------------------------------------------------
            count, st = visa.call(lib.write, sess, b"*IDN?\n")
            stats.check(st == StatusCode.success, f"write status {st!r}")
            data, st = visa.call(lib.read, sess, 4096)
            stats.check(
                st == StatusCode.success,
                f"a read of a complete message returns VI_SUCCESS, got {st!r}",
                rule="VPP-4.3 RULE 6.1.1",
            )
            stats.check(data.strip() == idn.encode(), "the read returned the *IDN? reply")

            # A short read must report max-count and leave the rest readable.
            lib.write(sess, b"*IDN?\n")
            data, st = visa.call(lib.read, sess, 4)
            stats.check(
                st == StatusCode.success_max_count_read,
                f"a short read reports VI_SUCCESS_MAX_CNT, got {st!r}",
                rule="VPP-4.3 RULE 6.1.2",
            )
            rest, _ = visa.call(lib.read, sess, 4096)
            stats.check(
                (data + rest).strip() == idn.encode(),
                "the remainder of a short read is still available",
                rule="VPP-4.3 RULE 6.1.2",
            )

            # -- status byte ------------------------------------------------
            stb, st = visa.call(lib.read_stb, sess)
            stats.check(st == StatusCode.success, f"read_stb status {st!r}")
            stats.note(f"status byte = {stb:#04x}")

            # -- trigger ----------------------------------------------------
            st = visa.status(lib.assert_trigger, sess, constants.TriggerProtocol.default)
            stats.check(st == StatusCode.success, f"assert_trigger {st!r}")
            # An "undefined header" or "trigger ignored" in the error queue
            # here means the trigger *arrived* and the instrument had nothing
            # to do with it, which is itself proof the message got through.
            visa.check_errors(inst, stats, "after assert_trigger")
            # VPP-4.3 6.1.7 lists VI_ERROR_INV_PROT among viAssertTrigger's
            # error codes, so refusing a protocol the transport cannot perform
            # is available and correct. It is not asserted here, because
            # NI-VISA accepts a non-default protocol over HiSLIP too and a
            # check that fails a shipping implementation on this point is
            # making a stronger claim than the clause supports.
            #
            # What is worth recording is that pyvisa-py answers differently on
            # its two transports: VI_ERROR_NSUP_OPER over HiSLIP, VI_SUCCESS
            # over VXI-11, for a protocol neither can actually perform.
            st = visa.status(lib.assert_trigger, sess, constants.TriggerProtocol.on)
            if st == StatusCode.success:
                stats.note(
                    f"a non-default trigger protocol was accepted ({st!r}); "
                    f"VPP-4.3 6.1.7 offers VI_ERROR_INV_PROT for this, and "
                    f"this backend refuses it on the other transport"
                )
            else:
                stats.check(
                    st
                    in (
                        StatusCode.error_nonsupported_operation,
                        StatusCode.error_invalid_protocol,
                    ),
                    "a non-default trigger protocol is refused cleanly",
                    rule="VPP-4.3 6.1.7",
                    detail=f"got {st!r}",
                )

            # -- clear ------------------------------------------------------
            stats.check(visa.status(lib.clear, sess) == StatusCode.success, "viClear")
            stats.check(
                inst.query("*IDN?").strip() == idn, "the session works after viClear"
            )

            # -- locking ----------------------------------------------------
            key, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
            stats.check(st == StatusCode.success, f"exclusive lock {st!r}",
                        rule="VPP-4.3 3.6.2.1")
            # VPP-4.3 leaves accessKey unused for an exclusive lock, so an
            # empty one is right -- but NI and R&S both hand back a generated
            # key regardless. Nothing depends on it being empty, so this is
            # recorded rather than failed.
            if key in ("", None, b""):
                stats.check(True, "an exclusive lock has an empty access key")
            else:
                stats.note(
                    f"this implementation returns an access key for an "
                    f"exclusive lock, where VPP-4.3 leaves it unused: {key!r}"
                )
            # Read through visa.call: a backend that does not implement this
            # attribute at all should be reported as a failed check, not kill
            # the run. Upstream pyvisa-py raises VI_ERROR_NSUP_ATTR here on
            # VXI-11 sessions, and losing the remaining 30 checks to it hides
            # much more than it shows.
            state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
            stats.check(
                st == StatusCode.success and state == constants.VI_EXCLUSIVE_LOCK,
                f"VI_ATTR_RSRC_LOCK_STATE reports the exclusive lock "
                f"(status {st!r}, value {state!r})",
                rule="VPP-4.3 3.6.2.1",
            )
            stats.check(visa.status(lib.unlock, sess) == StatusCode.success, "unlock")
            state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
            stats.check(
                st == StatusCode.success and state == constants.VI_NO_LOCK,
                f"VI_ATTR_RSRC_LOCK_STATE is clear after unlock "
                f"(status {st!r}, value {state!r})",
                rule="VPP-4.3 3.6.2.1",
            )

            key, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "smoke-key")
            stats.check(st == StatusCode.success, f"shared lock {st!r}")
            if args.protocol == "hislip":
                # bytes from NI-VISA, str from pyvisa-py. The type difference
                # is a disparity in its own right (docs/findings.md); this
                # check is about whether the key survived the round trip.
                returned = key.decode("ascii", "replace") if isinstance(key, bytes) else key
                stats.check(
                    returned == "smoke-key",
                    f"a shared lock returns its key, got {key!r}",
                    rule="VPP-4.3 3.6.2.1",
                )
            else:
                # VXI-11 locks are exclusive, per-link and non-nesting
                # (RULE B.6.72); the protocol has no shared-lock concept and
                # no field to carry a key. Requiring one here would be
                # requiring the backend to invent it.
                stats.note(
                    f"shared-lock key is not meaningful over VXI-11 "
                    f"(RULE B.6.72: locks are exclusive); got {key!r}"
                )
            # Not a bare unlock: an implementation that refused the shared
            # lock above has nothing to release, and viUnlock then raises
            # VI_ERROR_SESN_NLOCKED and takes the rest of the file with it.
            # R&S refuses shared locks over HiSLIP outright, which cost this
            # script its last 20 checks.
            visa.status(lib.unlock, sess)

            stats.check(
                visa.status(lib.unlock, sess) == StatusCode.error_session_not_locked,
                "unlocking an unlocked session is refused",
            )

            # -- remote/local -----------------------------------------------
            baseline = query_rate(inst)
            # VXI-11 carries only *addressed* remote/local operations:
            # device_remote (B.6.13) asserts REN and addresses the device,
            # device_local (B.6.14) sends GTL. There is no RPC for driving the
            # REN line on its own, so a backend refusing the unaddressed modes
            # is conforming, not deficient -- expecting success from all of
            # them was this check being wrong, not the backend.
            if args.protocol == "vxi11":
                expected_ok = {
                    constants.RENLineOperation.asrt_address,
                    constants.RENLineOperation.asrt_address_llo,
                    constants.RENLineOperation.address_gtl,
                    constants.RENLineOperation.deassert_gtl,
                }
            else:
                expected_ok = set(constants.RENLineOperation)

            for mode in constants.RENLineOperation:
                st = visa.status(lib.gpib_control_ren, sess, mode)
                if mode in expected_ok:
                    stats.check(st == StatusCode.success, f"REN {mode.name} -> {st!r}")
                else:
                    # Every implementation refuses these -- VXI-11 has no
                    # RPC for driving REN without addressing -- but they
                    # disagree about how to say so: pyvisa-py answers
                    # VI_ERROR_NSUP_OPER, NI and R&S both answer
                    # VI_ERROR_INVALID_MODE. Both are defensible refusals, so
                    # the check is that it *is* refused.
                    stats.check(
                        st in (
                            StatusCode.error_nonsupported_operation,
                            StatusCode.error_invalid_mode,
                        ),
                        f"REN {mode.name} is refused over VXI-11, got {st!r}",
                    )
            # The enum ends on a deassert, which would leave a real instrument
            # in local mode. Put it back under remote control.
            visa.status(
                lib.gpib_control_ren,
                sess,
                constants.RENLineOperation.asrt_address
                if args.protocol == "vxi11"
                else constants.RENLineOperation.asrt,
            )

            restored = query_rate(inst)
            stats.note(
                f"query rate {baseline:.0f}/s before the REN walk, "
                f"{restored:.0f}/s after"
            )
            stats.check(
                restored > baseline / 4,
                f"throughput survives the REN walk ({baseline:.0f}/s -> "
                f"{restored:.0f}/s). A large drop means the closing REN assert "
                f"did not take effect and the instrument was left in local mode",
            )

            # -- flush --------------------------------------------------------
            st = visa.status(
                lib.flush, sess, constants.BufferOperation.discard_read_buffer
            )
            # An unsupported operation must report VI_ERROR_NSUP_OPER, not
            # raise out of the library: a caller cannot catch what it has no
            # reason to expect, and a Python-level exception crossing the VISA
            # boundary is a contract break independent of whether flush is
            # implemented.
            stats.check(
                st in (StatusCode.success, StatusCode.error_nonsupported_operation),
                "viFlush reports a VISA status",
                rule="VPP-4.3 3.2.4",
                detail=f"got {st!r}",
            )

            # -- attributes ---------------------------------------------------
            attributes = COMMON_ATTRIBUTES
            if args.protocol == "hislip":
                attributes += HISLIP_ATTRIBUTES
            for name, expected in attributes:
                value, st = visa.call(lib.get_attribute, sess, getattr(RA, name))
                # One name for both outcomes. A check whose *name* changes
                # when it fails cannot be lined up against the same check in
                # another implementation's column, and the matrix then shows
                # the failure as "did not run" -- which reads as the opposite
                # of what happened.
                if st != StatusCode.success:
                    # RULE 5.1.12 requires these of any message-based INSTR
                    # resource, TCPIP named explicitly, so an unreadable one
                    # is a cited failure rather than an observation.
                    ok, detail = False, f"not readable ({st!r})"
                elif expected is None:
                    ok, detail = value is not None, f"= {value!r}"
                else:
                    ok, detail = value == expected, f"= {value!r} (wanted {expected!r})"
                stats.check(ok, f"{name} is readable", rule="VPP-4.3 5.1.12",
                            detail=detail)

            # VI_ATTR_INTF_INST_NAME is a human-readable string whose exact
            # wording is the backend's own; note it rather than assert it, or
            # every backend but pyvisa-py fails a cosmetic check.
            value, st = visa.call(lib.get_attribute, sess, RA.interface_instrument_name)
            stats.note(f"interface_instrument_name = {value!r}")

            for attribute, value, label in (
                (RA.tcpip_keepalive, True, "keepalive can be turned on"),
                (RA.termchar_enabled, True, "VI_ATTR_TERMCHAR_EN round trips"),
                (RA.send_end_enabled, False, "VI_ATTR_SEND_END_EN round trips"),
            ):
                _, set_st = visa.call(lib.set_attribute, sess, attribute, value)
                read, get_st = visa.call(lib.get_attribute, sess, attribute)
                stats.check(
                    set_st == StatusCode.success
                    and get_st == StatusCode.success
                    and read == value,
                    f"{label} (set {set_st!r}, read back {read!r})",
                )
                # Put it back: these are session-wide and the checks that
                # follow assume the defaults.
                visa.call(lib.set_attribute, sess, attribute, not value)

            # -- events -------------------------------------------------------
            try:
                inst.enable_event(visa.SRQ, visa.QUEUE)
                inst.disable_event(visa.SRQ, visa.QUEUE)
                stats.check(True, "SRQ events can be enabled and disabled")
            except Exception as exc:  # noqa: BLE001
                stats.error("SRQ events could not be enabled", exc)

            visa.check_errors(inst, stats, "at end of run")

        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
