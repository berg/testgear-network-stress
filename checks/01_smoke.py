#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every VISA operation the session implements, once, with a capability matrix.

Run this first. It is the quickest way to see whether anything is broken
outright, and the capability matrix tells you what the rest of the suite will
be able to exercise against this target.

Unlike most of the suite these checks share one session and run in order: the
point of a smoke test is that a single session survives being asked to do
everything, so "the session works after viClear" has to be the same session
that was cleared. What one check learns and the next one needs goes through
`STATE`, and a check whose predecessor did not get far enough skips rather
than failing for a reason that is not its own.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import harness, script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}

#: Values one check produces and a later one consumes. Not a convenience: it
#: is what lets a dependent check say *why* it could not run. Reading a
#: missing key would be an exception and a FAIL against the check that was
#: never given anything to check.
STATE: dict = {}


def inst():
    return CTX["session"]


def io():
    """The `(library, session)` pair the raw-status helpers want."""
    return CTX["session"].visalib, CTX["session"].session


def query_rate(count: int = 8) -> float:
    """Queries per second, for spotting an instrument left in local mode.

    A GPIB instrument in local mode still answers, just far more slowly, so a
    REN operation that does not stick shows up as throughput and not as an
    error. `VI_ATTR_GPIB_REN_STATE` would be the direct check, but the VISA
    spec defines it for GPIB resources, not TCPIP INSTR.
    """
    start = time.time()
    for _ in range(count):
        inst().query("*IDN?")
    return count / max(time.time() - start, 1e-6)


@contextlib.contextmanager
def SETUP(ctx):
    """One session for the whole file, opened before the first check.

    `testgear.script` enters this if a check module defines it, so the session
    is closed even when a check is abandoned by the watchdog still holding it.
    """
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")


# -- identification and raw I/O ---------------------------------------------
@check("*IDN? returns a non-empty identification string")
def check_idn():
    """The one check everything else assumes. Its answer is the oracle the
    read checks below compare against, so it is recorded, not just asserted."""
    idn = inst().query("*IDN?").strip()
    STATE["idn"] = idn
    visa.drain_errors(inst())
    assert idn, "the instrument answered *IDN? with an empty string"
    return idn


@check("viWrite reports VI_SUCCESS")
def check_write_status():
    lib, sess = io()
    _, st = visa.call(lib.write, sess, b"*IDN?\n")
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("a read of a complete message returns VI_SUCCESS", rule="VPP-4.3 RULE 6.1.1")
def check_read_status():
    lib, sess = io()
    data, st = visa.call(lib.read, sess, 4096)
    STATE["read"] = data
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("the read returned the *IDN? reply")
def check_read_content():
    """Separate from the status check above, because they fail for different
    reasons: a wrong status is a reporting bug, wrong bytes are a framing one,
    and a single row could not tell you which happened."""
    if "read" not in STATE:
        raise Skip("the read did not complete, so there is nothing to compare")
    got = STATE["read"].strip()
    assert got == STATE["idn"].encode(), f"got {got!r}"
    return f"got {got!r}"


@check("a short read reports VI_SUCCESS_MAX_CNT", rule="VPP-4.3 RULE 6.1.2")
def check_short_read_status():
    """A read smaller than the message must report max-count and leave the
    rest readable -- the second half is the check after this one."""
    lib, sess = io()
    lib.write(sess, b"*IDN?\n")
    data, st = visa.call(lib.read, sess, 4)
    STATE["short_read"] = data
    assert st == StatusCode.success_max_count_read, f"got {st!r}"
    return f"got {st!r}"


@check("the remainder of a short read is still available", rule="VPP-4.3 RULE 6.1.2")
def check_short_read_remainder():
    if "short_read" not in STATE:
        raise Skip("the short read did not complete, so there is no remainder")
    lib, sess = io()
    rest, _ = visa.call(lib.read, sess, 4096)
    got = (STATE["short_read"] + rest).strip()
    assert got == STATE["idn"].encode(), f"got {got!r}"
    return f"got {got!r}"


# -- status byte -------------------------------------------------------------
@check("viReadSTB reports VI_SUCCESS")
def check_read_stb():
    lib, sess = io()
    stb, st = visa.call(lib.read_stb, sess)
    if st == StatusCode.success:
        CTX["stats"].note(f"status byte = {stb:#04x}")
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


# -- trigger -----------------------------------------------------------------
@check("viAssertTrigger reports VI_SUCCESS")
def check_assert_trigger():
    """The error queue is drained afterwards whatever happened.

    An "undefined header" or "trigger ignored" landing in it means the trigger
    *arrived* and the instrument had nothing to do with it, which is itself
    proof the message got through -- so it is worth recording even on the path
    where the status was wrong.
    """
    lib, sess = io()
    try:
        st = visa.status(lib.assert_trigger, sess, constants.TriggerProtocol.default)
        assert st == StatusCode.success, f"got {st!r}"
        return f"got {st!r}"
    finally:
        visa.check_errors(inst(), CTX["stats"], "after assert_trigger")


@check("a non-default trigger protocol is refused cleanly", rule="VPP-4.3 6.1.7")
def check_non_default_trigger_protocol():
    """VPP-4.3 6.1.7 lists VI_ERROR_INV_PROT among viAssertTrigger's error
    codes, so refusing a protocol the transport cannot perform is available
    and correct. Accepting it is *not* asserted against, because NI-VISA
    accepts a non-default protocol over HiSLIP too, and a check that fails a
    shipping implementation on this point is making a stronger claim than the
    clause supports.

    What is worth recording is that pyvisa-py answers differently on its two
    transports: VI_ERROR_NSUP_OPER over HiSLIP, VI_SUCCESS over VXI-11, for a
    protocol neither can actually perform.

    Acceptance is a skip rather than silence, and under this check's own name.
    Recording only a note left the column blank where the others answered, and
    a blank cell reads as "not applicable" rather than "accepted, which the
    clause permits".
    """
    lib, sess = io()
    st = visa.status(lib.assert_trigger, sess, constants.TriggerProtocol.on)
    if st == StatusCode.success:
        raise Skip(
            f"it was accepted ({st!r}); VPP-4.3 6.1.7 offers "
            f"VI_ERROR_INV_PROT for this but does not require it, and "
            f"this backend refuses it on the other transport"
        )
    assert st in (
        StatusCode.error_nonsupported_operation,
        StatusCode.error_invalid_protocol,
    ), f"got {st!r}"
    return f"got {st!r}"


# -- clear -------------------------------------------------------------------
@check("viClear reports VI_SUCCESS")
def check_clear_status():
    lib, sess = io()
    st = visa.status(lib.clear, sess)
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("the session works after viClear")
def check_usable_after_clear():
    got = inst().query("*IDN?").strip()
    assert got == STATE["idn"], f"got {got!r}"
    return f"got {got!r}"


# -- locking -----------------------------------------------------------------
@check("viLock takes an exclusive lock", rule="VPP-4.3 3.6.2.1")
def check_exclusive_lock():
    lib, sess = io()
    key, st = visa.call(lib.lock, sess, constants.Lock.exclusive, 2000, None)
    STATE["exclusive_key"] = key
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("an exclusive lock has an empty access key")
def check_exclusive_lock_key():
    """VPP-4.3 leaves accessKey unused for an exclusive lock, so an empty one
    is right -- but NI and R&S both hand back a generated key regardless.

    Nothing in this suite depends on it being empty, so a returned key is not
    a failure. It is a skip rather than a bare note: a note leaves the cell
    blank for the implementations that generate a key, and blank reads as "not
    applicable" next to a column that answered.
    """
    if "exclusive_key" not in STATE:
        raise Skip("the exclusive lock was not taken, so there is no key")
    key = STATE["exclusive_key"]
    if key not in ("", None, b""):
        raise Skip(
            f"this implementation returns an access key for an exclusive "
            f"lock, where VPP-4.3 leaves it unused: {key!r}"
        )
    return f"got {key!r}"


@check("VI_ATTR_RSRC_LOCK_STATE reports the exclusive lock", rule="VPP-4.3 3.6.2.1")
def check_lock_state_attribute():
    """Read through `visa.call`, so a backend that does not implement the
    attribute at all is a failed check rather than a dead run. Upstream
    pyvisa-py raises VI_ERROR_NSUP_ATTR here on VXI-11 sessions, and losing the
    remaining thirty checks to it hides much more than it shows."""
    lib, sess = io()
    state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
    detail = f"status {st!r}, value {state!r}"
    assert st == StatusCode.success and state == constants.VI_EXCLUSIVE_LOCK, detail
    return detail


@check("viUnlock reports VI_SUCCESS")
def check_unlock_status():
    lib, sess = io()
    st = visa.status(lib.unlock, sess)
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("VI_ATTR_RSRC_LOCK_STATE is clear after unlock", rule="VPP-4.3 3.6.2.1")
def check_lock_state_cleared():
    lib, sess = io()
    state, st = visa.call(lib.get_attribute, sess, RA.resource_lock_state)
    detail = f"status {st!r}, value {state!r}"
    assert st == StatusCode.success and state == constants.VI_NO_LOCK, detail
    return detail


@check("viLock takes a shared lock")
def check_shared_lock():
    lib, sess = io()
    key, st = visa.call(lib.lock, sess, constants.Lock.shared, 2000, "smoke-key")
    STATE["shared_key"] = key
    if CTX["protocol"] == "vxi11":
        # VXI-11 locks are exclusive, per-link and non-nesting (RULE B.6.72);
        # the protocol has no shared-lock concept and no field to carry a key.
        # Requiring one would be requiring the backend to invent it, which is
        # why the key check below is HiSLIP-only rather than skipped here.
        CTX["stats"].note(
            f"shared-lock key is not meaningful over VXI-11 "
            f"(RULE B.6.72: locks are exclusive); got {key!r}"
        )
    assert st == StatusCode.success, f"got {st!r}"
    return f"got {st!r}"


@check("a shared lock returns its access key", rule="VPP-4.3 3.6.2.1",
       protocols=("hislip",))
def check_shared_lock_key():
    """HiSLIP only, and registered that way rather than skipped: VXI-11 has no
    shared lock to return a key for, so the check does not apply rather than
    failing to run.

    The key comes back as bytes from NI-VISA and str from pyvisa-py. That type
    difference is a disparity in its own right (docs/findings.md); this check
    is about whether the key survived the round trip.
    """
    if "shared_key" not in STATE:
        raise Skip("the shared lock was not taken, so there is no key")
    key = STATE["shared_key"]
    got = key.decode("ascii", "replace") if isinstance(key, bytes) else key
    assert got == "smoke-key", f"got {key!r}"
    return f"got {key!r}"


@check("unlocking an unlocked session is refused")
def check_unlock_when_unlocked():
    """The shared lock is released first, and its status ignored.

    Not a bare `unlock`: an implementation that refused the shared lock above
    has nothing to release, viUnlock then raises VI_ERROR_SESN_NLOCKED, and
    the rest of the file goes with it. R&S refuses shared locks over HiSLIP
    outright, which once cost this script its last twenty checks.
    """
    lib, sess = io()
    visa.status(lib.unlock, sess)
    st = visa.status(lib.unlock, sess)
    assert st == StatusCode.error_session_not_locked, f"got {st!r}"
    return f"got {st!r}"


# -- remote/local ------------------------------------------------------------
#: VXI-11 carries only *addressed* remote/local operations: device_remote
#: (B.6.13) asserts REN and addresses the device, device_local (B.6.14) sends
#: GTL. There is no RPC for driving the REN line on its own, so a backend
#: refusing the unaddressed modes is conforming, not deficient -- expecting
#: success from all of them was this check being wrong, not the backend.
VXI11_REN_MODES = frozenset(
    {
        constants.RENLineOperation.asrt_address,
        constants.RENLineOperation.asrt_address_llo,
        constants.RENLineOperation.address_gtl,
        constants.RENLineOperation.deassert_gtl,
    }
)


def _ren_baseline() -> None:
    """Measure throughput once, before the first REN operation."""
    if "ren_baseline" not in STATE:
        STATE["ren_baseline"] = query_rate()


def _ren_accepted(mode):
    def run():
        _ren_baseline()
        lib, sess = io()
        st = visa.status(lib.gpib_control_ren, sess, mode)
        assert st == StatusCode.success, f"got {st!r}"
        return f"got {st!r}"

    return run


def _ren_refused(mode):
    def run():
        """Every implementation refuses these -- VXI-11 has no RPC for driving
        REN without addressing -- but they disagree about how to say so:
        pyvisa-py answers VI_ERROR_NSUP_OPER, NI and R&S both answer
        VI_ERROR_INVALID_MODE. Both are defensible refusals, so the check is
        that it *is* refused."""
        _ren_baseline()
        lib, sess = io()
        st = visa.status(lib.gpib_control_ren, sess, mode)
        assert st in (
            StatusCode.error_nonsupported_operation,
            StatusCode.error_invalid_mode,
        ), f"got {st!r}"
        return f"got {st!r}"

    return run


def _register_ren_checks() -> None:
    """One check per REN mode, in the enum's own order.

    A mode VXI-11 cannot perform gets two registrations under two names --
    "is accepted" for HiSLIP, "is refused over VXI-11" for VXI-11 -- rather
    than one name whose wording changes with the transport. The matrix lines
    columns up by name, and a name that varies is a row that splits. Only one
    of the two is ever collected, so the enum's order survives into the
    report.
    """
    add = harness.registrar(globals())
    for mode in constants.RENLineOperation:
        vxi11_can = mode in VXI11_REN_MODES
        add(
            _ren_accepted(mode),
            f"REN {mode.name} is accepted",
            protocols=("vxi11", "hislip") if vxi11_can else ("hislip",),
        )
        if not vxi11_can:
            add(
                _ren_refused(mode),
                f"REN {mode.name} is refused over VXI-11",
                protocols=("vxi11",),
            )


_register_ren_checks()


@check("throughput survives the REN walk")
def check_throughput_after_ren():
    """The enum ends on a deassert, which would leave a real instrument in
    local mode, so this puts it back under remote control before measuring."""
    lib, sess = io()
    visa.status(
        lib.gpib_control_ren,
        sess,
        constants.RENLineOperation.asrt_address
        if CTX["protocol"] == "vxi11"
        else constants.RENLineOperation.asrt,
    )
    _ren_baseline()
    baseline = STATE["ren_baseline"]
    restored = query_rate()
    CTX["stats"].note(
        f"query rate {baseline:.0f}/s before the REN walk, "
        f"{restored:.0f}/s after"
    )
    detail = (
        f"{baseline:.0f}/s -> {restored:.0f}/s; a large drop means the closing "
        f"REN assert did not take effect and the instrument was left in local "
        f"mode"
    )
    assert restored > baseline / 4, detail
    return detail


# -- flush -------------------------------------------------------------------
@check("viFlush reports a VISA status", rule="VPP-4.3 3.2.4")
def check_flush():
    """An unsupported operation must report VI_ERROR_NSUP_OPER, not raise out
    of the library: a caller cannot catch what it has no reason to expect, and
    a Python-level exception crossing the VISA boundary is a contract break
    independent of whether flush is implemented."""
    lib, sess = io()
    st = visa.status(lib.flush, sess, constants.BufferOperation.discard_read_buffer)
    assert st in (
        StatusCode.success,
        StatusCode.error_nonsupported_operation,
    ), f"got {st!r}"
    return f"got {st!r}"


# -- attributes --------------------------------------------------------------
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
#: the backend, so they are only registered where they mean something.
HISLIP_ATTRIBUTES = (
    ("tcpip_is_hislip", True),
    ("tcpip_hislip_version", None),
    ("tcpip_hislip_max_message_kb", None),
    ("tcpip_hislip_overlap_enable", None),
)


def _attribute_readable(name, expected):
    def run():
        lib, sess = io()
        value, st = visa.call(lib.get_attribute, sess, getattr(RA, name))
        if st != StatusCode.success:
            # RULE 5.1.12 requires these of any message-based INSTR resource,
            # TCPIP named explicitly, so an unreadable one is a cited failure
            # rather than an observation.
            raise AssertionError(f"not readable ({st!r})")
        if expected is None:
            assert value is not None, f"= {value!r}"
            return f"= {value!r}"
        assert value == expected, f"= {value!r} (wanted {expected!r})"
        return f"= {value!r} (wanted {expected!r})"

    return run


def _register_attribute_checks() -> None:
    """One row per attribute, under one name for both outcomes.

    A check whose *name* changes when it fails cannot be lined up against the
    same check in another implementation's column, and the matrix then shows
    the failure as "did not run" -- which reads as the opposite of what
    happened. The observed value goes in the detail.
    """
    add = harness.registrar(globals())
    for attributes, protocols in (
        (COMMON_ATTRIBUTES, ("vxi11", "hislip")),
        (HISLIP_ATTRIBUTES, ("hislip",)),
    ):
        for name, expected in attributes:
            add(
                _attribute_readable(name, expected),
                f"{name} is readable",
                rule="VPP-4.3 5.1.12",
                protocols=protocols,
            )


_register_attribute_checks()


#: Attributes written and read back, with the value to try.
ROUND_TRIPS = (
    (RA.tcpip_keepalive, True, "keepalive can be turned on"),
    (RA.termchar_enabled, True, "VI_ATTR_TERMCHAR_EN round trips"),
    (RA.send_end_enabled, False, "VI_ATTR_SEND_END_EN round trips"),
)


def _round_trip(attribute, value, first):
    def run():
        lib, sess = io()
        if first:
            # VI_ATTR_INTF_INST_NAME is a human-readable string whose exact
            # wording is the backend's own. Noted rather than asserted, or
            # every backend but pyvisa-py fails a cosmetic check -- and noted
            # here because this is where it sits in the run.
            seen, _ = visa.call(lib.get_attribute, sess, RA.interface_instrument_name)
            CTX["stats"].note(f"interface_instrument_name = {seen!r}")
        _, set_st = visa.call(lib.set_attribute, sess, attribute, value)
        read, get_st = visa.call(lib.get_attribute, sess, attribute)
        detail = f"set {set_st!r}, read back {read!r}"
        try:
            assert (
                set_st == StatusCode.success
                and get_st == StatusCode.success
                and read == value
            ), detail
        finally:
            # Put it back whatever happened: these are session-wide and the
            # checks that follow assume the defaults.
            visa.call(lib.set_attribute, sess, attribute, not value)
        return detail

    return run


def _register_round_trip_checks() -> None:
    add = harness.registrar(globals())
    for index, (attribute, value, label) in enumerate(ROUND_TRIPS):
        add(_round_trip(attribute, value, first=index == 0), label)


_register_round_trip_checks()


# -- events ------------------------------------------------------------------
@check("SRQ events can be enabled and disabled")
def check_srq_enable_disable():
    """Both outcomes under one name, so the row lines up with the
    implementations that manage it."""
    try:
        inst().enable_event(visa.SRQ, visa.QUEUE)
        inst().disable_event(visa.SRQ, visa.QUEUE)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{type(exc).__name__}: {exc}") from exc
    return "viEnableEvent and viDisableEvent both returned without raising"


if __name__ == "__main__":
    script.run()
