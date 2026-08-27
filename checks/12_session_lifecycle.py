#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VPP-4.3 sections 3.2 to 3.4: the resource template every session inherits.

Unglamorous and cheap. These rules are not about instruments at all -- they are
about the VISA object model: which attributes exist, when they stop being
writeable, what a degenerate argument returns. They are worth checking exactly
because nobody exercises them by hand, so a backend can drift for years without
anyone noticing.

Where a rule turns out not to hold for a *transport* rather than for the
backend, the check says so and skips rather than failing. That distinction is
the thing this suite has had to learn repeatedly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants  # noqa: E402
from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

#: VPP-4.3 3.2.3 fixes this value exactly.
RSRC_SPEC_VERSION = 0x00700200

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


@check("VI_ATTR_RSRC_SPEC_VERSION is 00700200h", rule="VPP-4.3 3.2.3")
def check_spec_version():
    """3.2.3 gives this attribute one legal value.

    It names the VISA specification revision the resource implements, so a
    caller keying behaviour off it is keying off a promise. A backend
    reporting something else is claiming to implement a document that does
    not exist.
    """
    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.resource_spec_version
        )
        assert st == StatusCode.success, (
            f"VI_ATTR_RSRC_SPEC_VERSION is not readable ({st!r})"
        )
        assert value == RSRC_SPEC_VERSION, (
            f"expected {RSRC_SPEC_VERSION:#010x}, got "
            f"{value:#010x}" if isinstance(value, int) else f"got {value!r}"
        )
        return f"{value:#010x}"


@check("VI_ATTR_MAX_QUEUE_LENGTH is writeable before viEnableEvent",
       rule="VPP-4.3 3.2.5")
def check_queue_length_writeable():
    """3.2.5: read/write until the first viEnableEvent on that session.

    The queue length has to be settable before the queue exists, because
    afterwards there is a queue whose size would have to change underneath
    whatever is already in it.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, st = visa.call(lib.set_attribute, sess, RA.max_queue_length, 42)
        assert st == StatusCode.success, (
            f"setting VI_ATTR_MAX_QUEUE_LENGTH on a fresh session returned "
            f"{st!r}; 3.2.5 makes it writeable until viEnableEvent is called"
        )
        value, get_st = visa.call(lib.get_attribute, sess, RA.max_queue_length)
        assert get_st == StatusCode.success and value == 42, (
            f"it did not read back as set ({get_st!r}, {value!r})"
        )


@check("VI_ATTR_MAX_QUEUE_LENGTH is read-only after viEnableEvent",
       rule="VPP-4.3 3.2.6")
def check_queue_length_readonly():
    """3.2.6: read-only once the queue is in use.

    The interesting failure is the silent one -- accepting the write and
    ignoring it -- because a caller then believes it has a queue of the size
    it asked for and loses events at the real size.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        _, before = visa.call(lib.set_attribute, sess, RA.max_queue_length, 12)
        if before == StatusCode.error_nonsupported_attribute:
            # Nothing to say about when an attribute stops being writeable if
            # it was never writeable. 3.2.5's check already reports that.
            raise Skip(
                "VI_ATTR_MAX_QUEUE_LENGTH is not supported at all here, so "
                "3.2.6 has nothing to constrain"
            )
        inst.enable_event(visa.SRQ, visa.QUEUE)
        try:
            _, st = visa.call(lib.set_attribute, sess, RA.max_queue_length, 99)
            value, _ = visa.call(lib.get_attribute, sess, RA.max_queue_length)
            assert st != StatusCode.success, (
                f"VI_ATTR_MAX_QUEUE_LENGTH was still writeable after "
                f"viEnableEvent (it now reads {value!r}); 3.2.6 makes it "
                f"read-only from that point"
            )
            assert value == 12, (
                f"the write was refused with {st!r} but the value changed to "
                f"{value!r} anyway"
            )
            return f"refused with {st!r}"
        finally:
            with __import__("contextlib").suppress(Exception):
                inst.disable_event(visa.SRQ, visa.QUEUE)


@check("viClose(VI_NULL) returns VI_WARN_NULL_OBJECT", rule="VPP-4.3 3.3.2")
def check_close_null():
    """3.3.2: closing VI_NULL is a warning, not an error and not a crash.

    A tidy-up path that closes whatever handles it has, some of which may be
    VI_NULL, is entirely ordinary. The rule exists so that path does not need
    to special-case them.
    """
    rm = CTX["backend"].resource_manager()
    _, st = visa.call(rm.visalib.close, constants.VI_NULL)
    if st == visa.NOT_IMPLEMENTED:
        raise AssertionError("viClose(VI_NULL) raised instead of returning a status")
    assert st == StatusCode.warning_null_object, (
        f"expected VI_WARN_NULL_OBJECT, got {st!r}"
    )


@check("a string attribute reads back within 256 characters",
       rule="VPP-4.3 3.4.1")
def check_string_attribute_length():
    """3.4.1 caps a string attribute at 256 characters including the null.

    Checked against the resource name, which every session has and which is
    the longest string attribute in ordinary use.
    """
    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.resource_name
        )
        assert st == StatusCode.success, f"VI_ATTR_RSRC_NAME is not readable ({st!r})"
        text = value.decode() if isinstance(value, bytes) else str(value)
        assert len(text) < 256, (
            f"VI_ATTR_RSRC_NAME came back as {len(text)} characters; 3.4.1 "
            f"allows at most 256 including the null terminator"
        )
        return f"{len(text)} characters"


@check("an unsupported attribute state is refused, not accepted",
       rule="VPP-4.3 3.4.2")
def check_unsupported_attribute_state():
    """3.4.2: a valid-but-unsupportable state returns VI_ERROR_NSUP_ATTR_STATE.

    The failure mode this guards is the same silent one as 3.2.6: accepting a
    setting the resource cannot honour leaves the caller believing something
    about the session that is not true.

    Termination character is the vehicle here because every INSTR session has
    one and a value above 0xFF cannot be a character.
    """
    with open_inst() as inst:
        lib, sess = inst.visalib, inst.session
        original, _ = visa.call(lib.get_attribute, sess, RA.termchar)
        _, st = visa.call(lib.set_attribute, sess, RA.termchar, 0x1FF)
        value, _ = visa.call(lib.get_attribute, sess, RA.termchar)
        if original is not None:
            visa.call(lib.set_attribute, sess, RA.termchar, original)

        assert st != StatusCode.success, (
            f"a termination character of 0x1FF was accepted and the attribute "
            f"now reads {value!r}; a value that cannot be a character is not a "
            f"state the resource can honour"
        )
        return f"refused with {st!r}"


@check("the resource name reads back as something that reopens",
       rule="VPP-4.3 3.4.1")
def check_resource_name_roundtrip():
    """VI_ATTR_RSRC_NAME is documented as the canonical name of the resource.

    Canonical means usable: a name that cannot be handed back to viOpen is a
    label, not an identifier, and callers do use it that way -- it is how a
    session gets logged and later reopened.
    """
    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.resource_name
        )
        assert st == StatusCode.success, f"VI_ATTR_RSRC_NAME is not readable ({st!r})"
        name = value.decode() if isinstance(value, bytes) else str(value)

    rm = CTX["backend"].resource_manager()
    try:
        reopened = rm.open_resource(name, open_timeout=5000)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"the canonical name {name!r} could not be reopened: "
            f"{visa.visa_status(exc)}"
        ) from None
    try:
        reply = reopened.query("*IDN?")
        assert reply.strip(), "the reopened session answered nothing"
    finally:
        with __import__("contextlib").suppress(Exception):
            reopened.close()
    return name


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
            f"session lifecycle ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol=args.protocol)
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
