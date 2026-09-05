#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 section 3.7: enabling, queueing and delivering events.

The event machinery has more specified behaviour than any other part of the
resource template, and almost none of it involves an instrument. Whether
`viWaitOnEvent` dequeues an event whose type has since been disabled, or what
`VI_TMO_IMMEDIATE` does when the queue is empty, is decided entirely inside the
VISA implementation.

Service request is the event type used throughout, because it is the only one a
TCPIP INSTR session has. Where a rule needs an event that cannot be provoked
here, the check says so and skips.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def raise_srq(inst) -> None:
    """Provoke one service request through the instrument's status model."""
    for command in ("*CLS", "*ESE 1", "*SRE 32", "*OPC"):
        inst.write(command)


def quiet(inst) -> None:
    for command in ("*CLS", "*SRE 0", "*ESE 0"):
        with contextlib.suppress(Exception):
            inst.write(command)


@check("viEnableEvent with VI_HNDLR and no handler is refused",
       rule="VPP-4.3 3.7.6")
def check_enable_handler_without_handler():
    """3.7.6: enabling the callback mechanism with nothing installed is an
    error.

    Succeeding would leave a session enabled for a delivery mechanism that
    cannot deliver, so events are dropped and nothing says so.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        st = visa.status(lib.enable_event, sess, visa.SRQ, visa.HANDLER, constants.VI_NULL)
        if st == StatusCode.success:
            visa.status(lib.disable_event, sess, visa.SRQ, visa.HANDLER)
            raise AssertionError(
                "viEnableEvent(VI_HNDLR) succeeded with no handler installed; "
                "the session is now enabled for a mechanism that cannot deliver"
            )
        assert st == StatusCode.error_handler_not_installed, (
            f"expected VI_ERROR_HNDLR_NINSTALLED, got {st!r}"
        )
        return f"refused with {st!r}"


@check("VI_SUSPEND_HNDLR together with VI_HNDLR is refused",
       rule="VPP-4.3 3.7.13")
def check_mutually_exclusive_mechanisms():
    """3.7.13: the two callback modes bitwise-OR'd together is VI_ERROR_INV_MECH.

    They are mutually exclusive states -- deliver now versus defer -- so the
    combination has no meaning, and quietly picking one of them would leave
    the caller unable to tell which.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        both = constants.EventMechanism.suspend_handler | constants.EventMechanism.handler
        st = visa.status(lib.enable_event, sess, visa.SRQ, both, constants.VI_NULL)
        if st == StatusCode.success:
            with contextlib.suppress(Exception):
                visa.status(lib.disable_event, sess, visa.SRQ, both)
            raise AssertionError(
                "VI_SUSPEND_HNDLR | VI_HNDLR was accepted; they are mutually "
                "exclusive modes and the caller cannot tell which is in force"
            )
        assert st == StatusCode.error_invalid_mechanism, (
            f"expected VI_ERROR_INV_MECH, got {st!r}"
        )
        return f"refused with {st!r}"


@check("viInstallHandler refuses VI_ANY_HNDLR", rule="VPP-4.3 3.7.24")
def check_install_any_handler():
    """3.7.24: VI_ANY_HNDLR is a wildcard for *uninstalling*, and passing it to
    viInstallHandler returns VI_ERROR_INV_HNDLR_REF."""
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        try:
            _, st = visa.call(
                lib.install_visa_handler, sess, visa.SRQ, constants.VI_ANY_HNDLR, None
            )
        except Exception as exc:  # noqa: BLE001
            # Refusing by raising is not what the rule says, but it is a
            # refusal; report which it was rather than scoring it either way.
            return f"refused by raising {type(exc).__name__}"
        if st == visa.NOT_IMPLEMENTED:
            raise Skip("this backend does not expose viInstallHandler directly")
        if st is None:
            # A handler API that returns no status cannot be asked whether it
            # refused. Passing on that basis would be passing for the wrong
            # reason, which is worse than not running.
            raise Skip(
                "this backend's install_visa_handler reports no status code, "
                "so there is nothing to check 3.7.24 against"
            )
        assert st != StatusCode.success, (
            "installing VI_ANY_HNDLR as a handler succeeded; it is the "
            "uninstall wildcard, not a callable"
        )
        return f"refused with {st!r}"


@check("VI_TMO_IMMEDIATE does not suspend the caller", rule="VPP-4.3 3.7.20")
def check_wait_immediate():
    """3.7.20: with VI_TMO_IMMEDIATE, execution "SHALL NOT be suspended".

    A timing assertion, and necessarily so: the whole content of the rule is
    that the call returns rather than waits.
    """
    with open_inst() as inst:
        inst.enable_event(visa.SRQ, visa.QUEUE)
        try:
            inst.discard_events(visa.SRQ, visa.QUEUE)
            started = time.time()
            response = inst.wait_on_event(
                visa.SRQ, constants.VI_TMO_IMMEDIATE, capture_timeout=True
            )
            elapsed = time.time() - started
            assert response.timed_out, (
                "an empty queue returned an event for VI_TMO_IMMEDIATE"
            )
            assert elapsed < 0.25, (
                f"VI_TMO_IMMEDIATE suspended the caller for {elapsed:.2f}s"
            )
            return f"returned in {elapsed * 1000:.0f}ms"
        finally:
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.QUEUE)


@check("viWaitOnEvent dequeues an event whose type was since disabled",
       rule="VPP-4.3 3.7.23")
def check_dequeue_after_disable():
    """3.7.21 and 3.7.23: the queue is drained regardless of enabled state.

    An event already in the queue was delivered before anything was disabled;
    discarding it because the type is now disabled loses information the
    application already earned. This is the rule most likely to be got wrong
    by an implementation that treats "disabled" as "empty".
    """
    with open_inst() as inst:
        inst.enable_event(visa.SRQ, visa.QUEUE)
        try:
            inst.discard_events(visa.SRQ, visa.QUEUE)
            raise_srq(inst)
            arrived = False
            deadline = time.time() + 5.0
            while time.time() < deadline:
                response = inst.wait_on_event(visa.SRQ, 200, capture_timeout=True)
                if not response.timed_out:
                    arrived = True
                    break
            if not arrived:
                raise Skip("no service request arrived, so there is nothing queued")

            # Queue another, then disable the type before collecting it.
            #
            # The settle has to clear the transport's own delivery latency or
            # the event is not queued yet when the type is disabled, and the
            # check measures the wait rather than the rule. Over VXI-11 that
            # latency is about a second, because the interrupt RPC goes
            # unacknowledged -- a finding in its own right (docs/findings.md),
            # and one this check has to work around rather than trip over.
            raise_srq(inst)
            time.sleep(2.5 if CTX["protocol"] == "vxi11" else 0.4)
            inst.disable_event(visa.SRQ, visa.QUEUE)

            response = inst.wait_on_event(visa.SRQ, 500, capture_timeout=True)
            assert not response.timed_out, (
                "an event queued before the type was disabled could no longer "
                "be dequeued; 3.7.21 drains the queue regardless of enabled "
                "state, and the application had already earned that event"
            )
            return "dequeued after disabling"
        finally:
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.QUEUE)
            quiet(inst)


@check("discarded events do not come back", rule="VPP-4.3 3.7.21")
def check_discard_events():
    with open_inst() as inst:
        inst.enable_event(visa.SRQ, visa.QUEUE)
        try:
            raise_srq(inst)
            time.sleep(0.4)
            inst.discard_events(visa.SRQ, visa.QUEUE)
            response = inst.wait_on_event(visa.SRQ, 300, capture_timeout=True)
            assert response.timed_out, (
                "an event survived viDiscardEvents and was returned by the "
                "next viWaitOnEvent"
            )
        finally:
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.QUEUE)
            quiet(inst)
        return "the wait after viDiscardEvents timed out, as it should"


@check("a handler uninstalled while enabled stops being called",
       rule="VPP-4.3 3.7.26")
def check_uninstall_stops_delivery():
    """3.7.26: with no handler left installed, the callback mechanism for that
    session is disabled.

    Calling a handler after it has been uninstalled is the worst outcome
    available here -- the application has every reason to believe the callback
    and whatever it closes over are finished with.
    """
    with open_inst() as inst:
        calls: list[int] = []

        def handler(session, event_type, context, user_handle):
            calls.append(1)

        wrapped = inst.install_handler(visa.SRQ, handler)
        inst.enable_event(visa.SRQ, visa.HANDLER)
        try:
            raise_srq(inst)
            time.sleep(0.5)
            assert calls, "the handler was never called while installed"
            inst.disable_event(visa.SRQ, visa.HANDLER)
            inst.uninstall_handler(visa.SRQ, handler, wrapped)

            before = len(calls)
            raise_srq(inst)
            time.sleep(0.7)
            assert len(calls) == before, (
                f"the handler was called {len(calls) - before} more time(s) "
                f"after being uninstalled"
            )
            return f"{before} calls while installed, none after"
        finally:
            with contextlib.suppress(Exception):
                inst.disable_event(visa.SRQ, visa.HANDLER)
            with contextlib.suppress(Exception):
                inst.uninstall_handler(visa.SRQ, handler, wrapped)
            quiet(inst)


@check("enabling the queue twice is not an error", rule="VPP-4.3 3.7.10")
def check_enable_twice():
    """Enabling an already-enabled event type is idempotent, not a fault.

    Library code that enables defensively on entry has no way to know whether
    the caller already did, so an error here would make the defensive call
    itself the bug.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        first = visa.status(lib.enable_event, sess, visa.SRQ, visa.QUEUE, constants.VI_NULL)
        second = visa.status(lib.enable_event, sess, visa.SRQ, visa.QUEUE, constants.VI_NULL)
        with contextlib.suppress(Exception):
            visa.status(lib.disable_event, sess, visa.SRQ, visa.QUEUE)
        assert first == StatusCode.success, f"the first enable returned {first!r}"
        assert second in (
            StatusCode.success,
            StatusCode.success_event_already_enabled,
        ), f"the second enable returned {second!r}"
        return f"{second!r}"


if __name__ == "__main__":
    script.run()
