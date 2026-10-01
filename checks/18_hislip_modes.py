#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""IVI-6.1 §3: synchronized and overlapped mode, and how a client gets into each.

"All HiSLIP clients shall support both synchronized and overlapped mode"
(IVI-6.1 §3). Until the vendored server learned overlapped mode, only half of
that was testable: every session was synchronized, so a client that ignored the
server's choice and a client that honoured it looked the same, and
VI_ATTR_TCPIP_HISLIP_OVERLAP_EN could only be checked for being readable.

Three groups of checks:

- **Negotiation** (VPP-4.3 §5.1.2's description of the attribute, IVI-6.1
  §6.1 and §6.12.1). The
  server announces the mode a session starts in; the client may ask for the
  other at a device clear, and must then use whatever the server agrees to. The
  attribute "defaults to the mode suggested by the instrument", and "If
  changed, VISA will do a Device Clear operation to change the mode." Each of
  those needs a server in a particular mode, so these start their own mock and
  skip against real hardware.

- **Overlapped-mode wire rules** (IVI-6.1 §3.2, §3.2.2, §6.14). MessageIDs and the
  status query, read from the proxy's message log.

- **Overlapped-mode behaviour** through the VISA API: pipelined queries, the
  buffering rules, MAV. These run against real hardware too, when the
  instrument agrees to overlapped mode -- that is where a client meets an
  overlapped server most often.

The synchronized-mode counterparts of the wire rules are in
11_hislip_messages.py.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa import constants, errors  # noqa: E402

from testgear import script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

# IVI-6.1 §2.5, Table 4.
MSG_INITIALIZE_RESPONSE = 1
MSG_DATA, MSG_DATA_END = 6, 7
MSG_DEVICE_CLEAR_COMPLETE, MSG_DEVICE_CLEAR_ACKNOWLEDGE = 8, 9
MSG_TRIGGER = 12
MSG_ASYNC_MAXIMUM_MESSAGE_SIZE = 15
MSG_ASYNC_DEVICE_CLEAR = 19
MSG_ASYNC_STATUS_QUERY = 21
MSG_ASYNC_DEVICE_CLEAR_ACKNOWLEDGE = 23

#: Bit 0 of InitializeResponse's control code and of the feature bitmap
#: (IVI-6.1 §6.1, and Table 31 in §6.12.1).
OVERLAPPED = 0x01

#: IVI-6.1 §3.2.1 / §3.2.2: where both ends' counters start, and their step.
FIRST_ID = 0xFFFFFF00
STEP = 2
#: IVI-6.1 §6.14: what a status query quotes before anything has been delivered.
BEFORE_FIRST_ID = 0xFFFFFEFE

STB_MAV = 0x10

OVERLAP_EN = constants.ResourceAttribute.tcpip_hislip_overlap_enable
MAX_MESSAGE_KB = constants.ResourceAttribute.tcpip_hislip_max_message_kb

CTX: dict = {}


# ---------------------------------------------------------------- plumbing

def needs_mock():
    if CTX.get("server") is None:
        raise Skip("needs the mock server, to choose which modes the server offers")
    if CTX["args"].no_proxy:
        raise Skip("needs the proxy's message log, and this run has --no-proxy")


@contextlib.contextmanager
def offering(modes: str):
    """A fresh mock offering `modes`, and one session to it.

    Fresh rather than the run's own server because the mode a session starts
    in is decided at Initialize, by the server, and these checks need each of
    the three kinds.
    """
    needs_mock()
    from testgear.server import mock_server

    with mock_server(hislip_modes=modes) as srv:
        with open_inst(srv.resource("hislip")) as inst:
            yield srv, inst


def open_inst(resource: str | None = None):
    return visa.session(
        CTX["backend"], resource or CTX["resource"], timeout=CTX["timeout"]
    )


def overlap_en(inst) -> bool:
    """VI_ATTR_TCPIP_HISLIP_OVERLAP_EN, or fail: it is a required attribute
    (VPP-4.3 RULE 5.1.17), and every check here leans on it."""
    value, st = visa.call(inst.visalib.get_attribute, inst.session, OVERLAP_EN)
    assert st == constants.StatusCode.success and value is not None, (
        f"VI_ATTR_TCPIP_HISLIP_OVERLAP_EN is not readable ({st!r})"
    )
    return bool(value)


def set_overlap_en(inst, overlapped: bool):
    return visa.status(
        inst.visalib.set_attribute,
        inst.session,
        OVERLAP_EN,
        constants.VI_TRUE if overlapped else constants.VI_FALSE,
    )


def mode_name(overlapped: bool) -> str:
    return "overlapped" if overlapped else "synchronized"


def client_says(inst) -> str:
    """For failure messages: what the client believes, which is usually the
    first thing to know when its behaviour does not match the server's mode."""
    try:
        return f"the client reports {mode_name(overlap_en(inst))} mode"
    except AssertionError:
        return "the client's mode attribute is unreadable"


@contextlib.contextmanager
def overlapped_session():
    """An overlapped session, or skip.

    Against the mock, from a server that starts sessions overlapped: the client
    is not asked, so these checks hold it to following the server. Against a
    real instrument there is no choosing what it prefers, so ask with the
    attribute and skip if it will not go.
    """
    if CTX.get("server") is None:
        with open_inst() as inst:
            got = visa.set_hislip_overlap(inst, True)
            if got is not True:
                raise Skip(
                    "the instrument did not agree to overlapped mode "
                    f"(VI_ATTR_TCPIP_HISLIP_OVERLAP_EN reads {got!r} after asking)"
                )
            with _io_errors_fail(inst):
                yield None, inst
        return
    with offering("prefer-overlapped") as (srv, inst):
        with _io_errors_fail(inst):
            yield srv, inst


@contextlib.contextmanager
def _io_errors_fail(inst):
    """A VISA error in an overlapped session is the finding, not a crash in
    the check: the commonest is a timeout from a client applying the
    synchronized discard rule (§3.1.2 rule 1) to replies numbered by an
    overlapped server. Say so, with what the client thinks its mode is."""
    try:
        yield
    except errors.VisaIOError as exc:
        raise AssertionError(
            f"{visa.visa_status(exc)} in an overlapped session; {client_says(inst)}"
        ) from None


def messages(srv, *types: int, sender: str | None = None) -> list[dict]:
    return [
        m
        for m in srv.hislip_messages()
        if m["message_type"] in types and (sender is None or m["from"] == sender)
    ]


def client_ids(srv) -> list[int]:
    """MessageIDs the client put on Data, DataEND and Trigger, in order."""
    return [
        m["message_parameter"]
        for m in messages(srv, MSG_DATA, MSG_DATA_END, MSG_TRIGGER, sender="client")
    ]


def agreed_mode(srv, what: str) -> bool:
    """The mode the last DeviceClearAcknowledge settled on, or fail if the
    client never got that far -- which is a client failure, not a server one:
    `what` names the operation that should have run the handshake."""
    agreed = messages(srv, MSG_DEVICE_CLEAR_ACKNOWLEDGE, sender="server")
    assert agreed, (
        f"{what} did not complete a device clear (no DeviceClearAcknowledge), "
        f"so the mode was never renegotiated"
    )
    return bool(agreed[-1]["control_code"] & OVERLAPPED)


def status_query_ids(srv) -> list[int]:
    return [m["message_parameter"] for m in messages(srv, MSG_ASYNC_STATUS_QUERY)]


def wait_for_replies(srv, count: int, timeout: float = 3.0) -> None:
    """Until the server has sent `count` DataENDs, so a check about buffered
    replies is not racing their arrival. Against hardware, a pause instead."""
    if srv is None:
        time.sleep(0.5)
        return
    srv.wait_for(
        lambda _observed: len(messages(srv, MSG_DATA_END, sender="server")) >= count,
        timeout=timeout,
    )


def a_reply(inst) -> str:
    return inst.read().strip()


def idn_and_opc(inst) -> tuple[str, str]:
    """The two answers the behavioural checks tell apart. *IDN? and *OPC? are
    IEEE 488.2 common queries, so every instrument answers both, and
    differently."""
    return inst.query("*IDN?").strip(), inst.query("*OPC?").strip()


# ------------------------------------------------------------- negotiation

@check("VI_ATTR_TCPIP_HISLIP_OVERLAP_EN starts as the mode the server announced",
       rule="VPP-4.3 §5.1.2")
def check_attribute_defaults_to_server_mode():
    """VPP-4.3 §5.1.2, the attribute's description: its "value defaults to the
    mode suggested by the instrument on HiSLIP connection"; Appendix A, §A.1
    gives the default as "Preference returned by device." The instrument suggests it
    in InitializeResponse bit 0 -- IVI-6.1 §6.1: "The server uses this field to
    indicate if it is initially in overlapped or synchronous mode."

    A client that hardwires VI_FALSE passes against every synchronized-only
    server and is wrong about the session against every other.
    """
    seen = []
    for modes, expected in (
        ("synchronized", False),
        ("prefer-synchronized", False),
        ("prefer-overlapped", True),
    ):
        with offering(modes) as (srv, inst):
            init = messages(srv, MSG_INITIALIZE_RESPONSE, sender="server")
            assert init, "no InitializeResponse in the log"
            announced = bool(init[0]["control_code"] & OVERLAPPED)
            assert announced == expected, (
                f"suite error: a {modes} server announced "
                f"{mode_name(announced)}"
            )
            got = overlap_en(inst)
            assert got == expected, (
                f"the server started the session {mode_name(expected)}, but "
                f"VI_ATTR_TCPIP_HISLIP_OVERLAP_EN reads {got!r}"
            )
            seen.append(f"{modes}={int(got)}")
    return ", ".join(seen)


@check("VI_ATTR_TCPIP_HISLIP_OVERLAP_EN is writable", rule="VPP-4.3 §5.1.2")
def check_attribute_is_writable():
    """VPP-4.3 §5.1.2's HiSLIP attribute table lists it as "R/W Local". It is the
    only way a VISA caller has to choose a mode."""
    with offering("prefer-synchronized") as (_srv, inst):
        results = []
        for value in (False, True, False):
            st = set_overlap_en(inst, value)
            assert st == constants.StatusCode.success, (
                f"setting VI_ATTR_TCPIP_HISLIP_OVERLAP_EN to {int(value)} on a "
                f"server that supports both modes returned {st!r}"
            )
            results.append(int(value))
        return f"set {results} against a server with both modes"


@check("changing the mode performs the device clear handshake, asking for it",
       rule="VPP-4.3 §5.1.2")
def check_change_runs_device_clear():
    """VPP-4.3 §5.1.2, the attribute's description: "If changed, VISA will do
    a Device Clear operation to change the mode." IVI-6.1 §6.12.1 is how a
    clear changes it: "The client indicates values that it requests in
    the DeviceClearComplete message."

    Checked in both directions, each against a server that prefers the other
    mode, so the request cannot be the server's own proposal copied back.
    """
    done = []
    for modes, wanted in (("prefer-synchronized", True), ("prefer-overlapped", False)):
        with offering(modes) as (srv, inst):
            srv.reset()
            set_overlap_en(inst, wanted)
            assert messages(srv, MSG_ASYNC_DEVICE_CLEAR, sender="client"), (
                f"changing the attribute to {int(wanted)} sent no AsyncDeviceClear"
            )
            complete = messages(srv, MSG_DEVICE_CLEAR_COMPLETE, sender="client")
            assert complete, "the device clear was never completed"
            asked = bool(complete[-1]["control_code"] & OVERLAPPED)
            assert asked == wanted, (
                f"asked for {mode_name(wanted)} through the attribute, but "
                f"DeviceClearComplete requested {mode_name(asked)}"
            )
            done.append(f"{modes}: asked {mode_name(wanted)}")
    return "; ".join(done)


@check("after a mode change the session is in the agreed mode",
       rule="IVI-6.1 §6.12.1")
def check_change_takes_effect():
    """§6.12.1: "The client shall use the values specified by the server in the
    DeviceClearAcknowledge message."

    Read back through the attribute, and confirmed on the wire by the one
    client behaviour that differs between the modes: the MessageID an
    AsyncStatusQuery quotes. Synchronized mode quotes the client's own last
    message (§6.14); overlapped quotes the server's last delivered one
    (§3.2.2 item 1). A write-only command first makes those two numbers
    differ.

    §6.14 contradicts itself here: one sentence has clients quote "the most
    recently sent Data, DataEND, or Trigger message" with no mode named. This
    follows the overlap-specific text, §3.2.2 item 1 and §6.14's own field
    description, which NI-VISA, R&S VISA and Keysight all follow too.
    """
    done = []
    for modes, wanted in (("prefer-synchronized", True), ("prefer-overlapped", False)):
        with offering(modes) as (srv, inst):
            set_overlap_en(inst, wanted)
            agreed = agreed_mode(srv, "changing the attribute")
            assert agreed == wanted, (
                f"the server agreed to {mode_name(agreed)} when asked for "
                f"{mode_name(wanted)}: either the client asked for the wrong "
                f"mode (see the handshake check) or the server is wrong"
            )
            got = overlap_en(inst)
            assert got == wanted, (
                f"the server agreed to {mode_name(wanted)}, but "
                f"VI_ATTR_TCPIP_HISLIP_OVERLAP_EN reads {got!r}"
            )
            srv.reset()
            inst.write("*CLS")
            inst.query("*IDN?")
            inst.read_stb()
            mine = client_ids(srv)[-1]
            theirs = messages(srv, MSG_DATA_END, sender="server")[-1]["message_parameter"]
            quoted = status_query_ids(srv)[-1]
            expected = theirs if wanted else mine
            assert quoted == expected, (
                f"in {mode_name(wanted)} mode the status query should quote "
                f"{expected:#010x} ({'the reply' if wanted else 'the request'}); "
                f"it quoted {quoted:#010x}"
            )
            done.append(f"{mode_name(wanted)} in effect")
    return "; ".join(done)


@check("a server without overlapped mode keeps the session synchronized",
       rule="IVI-6.1 §6.12.1")
def check_refusal_is_honoured():
    """§3: servers "shall support either synchronized or overlapped mode or
    both", so asking is not getting. §6.12.1: the client "shall use the
    values specified by the server in the DeviceClearAcknowledge message."

    The setter may report the refusal or not -- VPP-4.3 does not say -- but
    afterwards the attribute and the client's behaviour must both be
    synchronized. A client that believes it is overlapped against a
    synchronized server stops applying §3.1.2 and will hand a caller replies
    that belong to a different query.
    """
    with offering("synchronized") as (srv, inst):
        srv.reset()
        st = set_overlap_en(inst, True)
        assert not agreed_mode(srv, "asking for overlapped"), (
            "suite error: a synchronized-only server agreed to overlapped"
        )
        got = overlap_en(inst)
        assert got is False, (
            f"the server answered the request with synchronized, but "
            f"VI_ATTR_TCPIP_HISLIP_OVERLAP_EN reads {got!r} (the set returned "
            f"{st!r})"
        )
        srv.reset()
        inst.write("*CLS")
        inst.query("*IDN?")
        inst.read_stb()
        mine = client_ids(srv)[-1]
        quoted = status_query_ids(srv)[-1]
        assert quoted == mine, (
            f"the status query quoted {quoted:#010x}, not the client's last "
            f"MessageID {mine:#010x}: the client is behaving as overlapped "
            f"against a server that refused it"
        )
        return f"set returned {st!r}; the session stayed synchronized"


@check("viClear keeps the session in its current mode", rule="VPP-4.3 §5.1.2")
def check_clear_keeps_mode():
    """Derived, not stated: no clause says in so many words what viClear must
    ask for. VPP-4.3 §5.1.2: "If disabled, the connection uses ‘Synchronous’
    mode ... If enabled, the connection uses ‘Overlapped’ mode". Every device
    clear renegotiates the mode (IVI-6.1 §6.12.1), so to keep that true every
    viClear has to ask for the one the attribute names. The server proposes its *own* preference in
    AsyncDeviceClearAcknowledge; a client that answers with that proposal
    instead of the caller's choice silently changes mode on the first viClear.

    Tested against servers preferring the opposite of the session's mode, which
    is the only arrangement where the two answers differ.
    """
    done = []
    for modes, mode in (("prefer-synchronized", True), ("prefer-overlapped", False)):
        with offering(modes) as (srv, inst):
            set_overlap_en(inst, mode)
            assert overlap_en(inst) == mode, (
                f"could not get into {mode_name(mode)} mode to begin with"
            )
            srv.reset()
            inst.clear()
            proposal = messages(srv, MSG_ASYNC_DEVICE_CLEAR_ACKNOWLEDGE, sender="server")
            assert proposal and bool(proposal[-1]["control_code"] & OVERLAPPED) != mode, (
                "suite error: the server proposed the session's own mode"
            )
            complete = messages(srv, MSG_DEVICE_CLEAR_COMPLETE, sender="client")
            assert complete, "viClear did not complete the device clear"
            asked = bool(complete[-1]["control_code"] & OVERLAPPED)
            assert asked == mode, (
                f"the session was {mode_name(mode)}; viClear asked for "
                f"{mode_name(asked)}, which is the server's proposal, not the "
                f"caller's choice"
            )
            got = overlap_en(inst)
            assert got == mode, (
                f"after viClear VI_ATTR_TCPIP_HISLIP_OVERLAP_EN reads {got!r}"
            )
            done.append(f"{mode_name(mode)} kept")
    return "; ".join(done)


@check("each session negotiates its own mode", rule="IVI-6.1 §6.12.1")
def check_modes_are_per_session():
    """§6.12.1, Table 31: the overlapped bit is "Related to current connection".
    Switching one session must not switch another to the same instrument --
    which it would if a client kept the mode anywhere shared. This assumes one
    HiSLIP connection per VISA session, which every implementation here does
    but no clause states.

    Judged on the wire, not only by the attribute: each session's status query
    has to quote by its own mode's rule (see the check on a mode change taking
    effect). An attribute that merely stores what was written would otherwise
    pass this without either session changing at all.
    """
    needs_mock()
    from testgear.server import mock_server

    with mock_server(hislip_modes="prefer-synchronized") as srv:
        resource = srv.resource("hislip")
        with open_inst(resource) as switched, open_inst(resource) as other:
            srv.reset()
            set_overlap_en(switched, True)
            assert agreed_mode(srv, "switching the first session"), (
                "the first session did not get into overlapped mode"
            )
            assert overlap_en(other) is False, (
                "switching one session to overlapped changed the other's "
                "VI_ATTR_TCPIP_HISLIP_OVERLAP_EN too"
            )
            for inst, overlapped in ((switched, True), (other, False)):
                srv.reset()
                inst.write("*CLS")
                inst.query("*IDN?")
                inst.read_stb()
                mine = client_ids(srv)[-1]
                theirs = messages(srv, MSG_DATA_END, sender="server")[-1]["message_parameter"]
                quoted = status_query_ids(srv)[-1]
                expected = theirs if overlapped else mine
                assert quoted == expected, (
                    f"the {mode_name(overlapped)} session's status query quoted "
                    f"{quoted:#010x}, expected {expected:#010x}"
                )
            return "one overlapped, one synchronized, side by side"


# ---------------------------------------------- overlapped-mode wire rules

@check("in overlapped mode the client's MessageIDs start at 0xFFFFFF00 and step by two",
       rule="IVI-6.1 §3.2.2")
def check_overlapped_message_ids():
    """§3.2.2: "Clients shall maintain a MessageID count that is initially set
    to 0xffff ff00. When clients send Data, DataEND or Trigger messages, they
    shall set the MessageID field ... and increment the MessageID by two".

    The same rule as synchronized mode, restated for this one -- a client with
    a separate overlapped code path has a separate place to get it wrong.
    """
    with overlapped_session() as (srv, inst):
        needs_mock()
        srv.reset()
        inst.write("*CLS")
        inst.query("*IDN?")
        inst.query("*OPC?")
        ids = client_ids(srv)
        expected = [(FIRST_ID + STEP * i) & 0xFFFFFFFF for i in range(len(ids))]
        assert len(ids) >= 3, f"expected 3 client messages, saw {len(ids)}"
        assert ids == expected, (
            f"MessageIDs {[hex(i) for i in ids]}, expected "
            f"{[hex(i) for i in expected]}; {client_says(inst)}"
        )
        return " ".join(f"{i:#010x}" for i in ids)


@check("in overlapped mode a Trigger takes a MessageID too", rule="IVI-6.1 §3.2.2")
def check_overlapped_trigger_id():
    """§3.2.2 counts Trigger with Data and DataEND. "In overlap mode, the
    MessageID is only used for locking" (also §3.2.2), so a mis-numbered
    Trigger does not desynchronise replies; what it breaks is lock release,
    whose MessageID "designates the last message to be completed before the
    release takes place" (§6.5)."""
    with overlapped_session() as (srv, inst):
        needs_mock()
        srv.reset()
        inst.write("*CLS")
        st = visa.status(inst.visalib.assert_trigger, inst.session,
                         constants.TriggerProtocol.default)
        if st != constants.StatusCode.success:
            raise Skip(f"viAssertTrigger returned {st!r}")
        inst.write("*CLS")
        triggers = messages(srv, MSG_TRIGGER, sender="client")
        assert triggers, "viAssertTrigger sent no Trigger message"
        ids = client_ids(srv)
        expected = [(FIRST_ID + STEP * i) & 0xFFFFFFFF for i in range(len(ids))]
        assert ids == expected, (
            f"MessageIDs {[hex(i) for i in ids]} across write, trigger, write; "
            f"expected {[hex(i) for i in expected]}"
        )
        return " ".join(f"{i:#010x}" for i in ids)


@check("in overlapped mode the MessageID restarts after a device clear",
       rule="IVI-6.1 §3.2.2")
def check_overlapped_ids_reset_on_clear():
    """§3.2.2: "The MesssageID is reset after device clear" (sic). The server
    resets its own counter at the same point (§3.2.1 item 1)."""
    with overlapped_session() as (srv, inst):
        needs_mock()
        srv.reset()
        for _ in range(3):
            inst.query("*IDN?")
        before = client_ids(srv)
        inst.clear()
        srv.reset()
        inst.query("*IDN?")
        after = client_ids(srv)
        assert after and after[0] == FIRST_ID, (
            f"the first MessageID after a device clear was "
            f"{after[0]:#010x}, expected {FIRST_ID:#010x}; {client_says(inst)}"
            if after else "no message followed the device clear"
        )
        return f"{before[-1]:#010x} -> clear -> {after[0]:#010x}"


@check("in overlapped mode a status query quotes 0xFFFFFEFE before any reply",
       rule="IVI-6.1 §6.14")
def check_overlapped_status_query_before_io():
    """§6.14: "After the Initialization and Device Clear transactions, the
    client shall use the MessageID = 0xfffffefe (0xffffff00-2) in the
    AsyncStatusQuery message." Checked after both."""
    with overlapped_session() as (srv, inst):
        needs_mock()
        srv.reset()
        inst.read_stb()
        after_init = status_query_ids(srv)
        inst.query("*IDN?")
        inst.clear()
        srv.reset()
        inst.read_stb()
        after_clear = status_query_ids(srv)
        assert after_init and after_init[-1] == BEFORE_FIRST_ID, (
            f"after initialization the status query quoted "
            f"{after_init[-1]:#010x}, expected {BEFORE_FIRST_ID:#010x}"
            if after_init else "viReadSTB sent no AsyncStatusQuery"
        )
        assert after_clear and after_clear[-1] == BEFORE_FIRST_ID, (
            f"after a device clear the status query quoted "
            f"{after_clear[-1]:#010x}, expected {BEFORE_FIRST_ID:#010x}"
        )
        return f"{BEFORE_FIRST_ID:#010x} after init and after clear"


@check("in overlapped mode a status query quotes the last reply delivered",
       rule="IVI-6.1 §3.2.2")
def check_overlapped_status_query_id():
    """§3.2.2 item 1: "In overlap mode, when sending AsyncStatusQuery, the client shall place
    the MessageID of the most recent message that has been entirely delivered
    to the client in the message parameter."

    That is the *server's* number, and §6.14.2 computes MAV from it. A client
    quoting its own last request -- the synchronized rule -- makes MAV come out
    set for as long as the session lives. (One sentence of §6.14 says
    otherwise for every mode; see check_change_takes_effect.)
    """
    with overlapped_session() as (srv, inst):
        needs_mock()
        srv.reset()
        inst.write("*CLS")
        inst.query("*IDN?")
        inst.query("*OPC?")
        inst.read_stb()
        last_reply = messages(srv, MSG_DATA_END, sender="server")[-1]["message_parameter"]
        last_request = client_ids(srv)[-1]
        quoted = status_query_ids(srv)[-1]
        assert quoted == last_reply, (
            f"the status query quoted {quoted:#010x}; the last reply delivered "
            f"was {last_reply:#010x}"
            + (" -- that is the client's own last MessageID, the synchronized "
               "rule" if quoted == last_request else "")
            + f"; {client_says(inst)}"
        )
        return f"quoted {quoted:#010x}"


@check("in overlapped mode a reply is accepted whatever MessageID it carries",
       rule="IVI-6.1 §3")
def check_overlapped_reply_ids_not_matched():
    """§3: clients "shall support both synchronized and overlapped mode", and
    §3.2 says what overlapped is: "Buffers are only cleared by a device
    clear." In overlapped mode a reply's MessageID is the server's own count
    (§3.2.1), so a client that matches it against the request -- the
    synchronized client's job under §3.1.2 rule 1, which is scoped to that mode
    -- clears a buffered reply without a device clear.

    A write-only command first puts the two counters out of step, which is all
    it takes; no fault injection is needed, so this holds against hardware.
    """
    with overlapped_session() as (_srv, inst):
        inst.write("*CLS")
        inst.write("*CLS")
        idn, opc = idn_and_opc(inst)
        assert idn and opc == "1", (
            f"replies after the counters diverged were {idn!r}, {opc!r}; "
            f"{client_says(inst)}"
        )
        return f"*IDN? -> {idn[:40]!r}"


@check("in overlapped mode a reply with an unexpected MessageID is still delivered",
       rule="IVI-6.1 §3")
def check_overlapped_skewed_reply_delivered():
    """The fault-injected form of the check above: the proxy shifts the next
    DataEND's MessageID by an amount no counter arithmetic produces. That
    makes the *server* break §3.2.1 item 2, and no clause says what a client
    does with a non-conforming message. What this rests on is §3's "All HiSLIP
    clients shall support both synchronized and overlapped mode", and §3.2's
    description of that mode, "Buffers are only cleared by a device clear":
    in synchronized mode
    11_hislip_messages.py requires this message discarded (§3.1.2 rule 1); in
    overlapped mode nothing licenses discarding it."""
    with overlapped_session() as (srv, inst):
        needs_mock()
        expected = inst.query("*IDN?").strip()
        inst.timeout = 2000
        srv.reset()
        skew = 0x1000
        with srv.hislip_faults(skew_data_end_id=skew):
            try:
                got = inst.query("*IDN?").strip()
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"the reply with a shifted MessageID was not delivered "
                    f"({visa.visa_status(exc)}); {client_says(inst)}"
                ) from None
        assert got == expected, f"got {got!r}, expected {expected!r}"
        # The log holds what the client was sent, after the rewrite: without
        # this a disarmed fault (TESTGEAR_DISABLE_FAULTS) would pass unseen.
        sent = messages(srv, MSG_DATA_END, sender="server")
        shifted = (FIRST_ID + STEP + skew) & 0xFFFFFFFF
        assert sent and sent[-1]["message_parameter"] == shifted, (
            "the MessageID was not shifted, so the fault did not fire and "
            "this proves nothing"
        )
        return f"delivered with MessageID {shifted:#010x}"


@check("in overlapped mode a reply in several messages, each with its own id, is reassembled",
       rule="IVI-6.1 §3")
def check_overlapped_chunked_reply():
    """§3.2.1 item 2 has the server number *every* Data and DataEND, so a
    reply in several messages carries several MessageIDs. That is a server
    rule. What binds the client is §3, "All HiSLIP clients shall support both
    synchronized and overlapped mode", with overlapped mode as §3.2 describes
    it: "Buffers are only cleared by a device clear." A client that insists
    the pieces of one reply
    share an id -- as they may in synchronized mode, §3.1.1 -- loses every
    multi-message reply.

    The server only splits a reply larger than the client's maximum, so the
    client is asked for a 1 KB maximum through
    VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB.
    """
    with overlapped_session() as (srv, inst):
        needs_mock()
        size = 5000
        srv.big_reply(size)
        st = visa.status(inst.visalib.set_attribute, inst.session, MAX_MESSAGE_KB, 1)
        if st != constants.StatusCode.success:
            raise Skip(f"VI_ATTR_TCPIP_HISLIP_MAX_MESSAGE_KB could not be set ({st!r})")
        srv.reset()
        inst.timeout = 3000
        reply = inst.query("TEST:BIG?").strip()
        pieces = messages(srv, MSG_DATA, MSG_DATA_END, sender="server")
        if len(pieces) < 2:
            raise Skip(
                "the reply arrived in one message: the client did not pass its "
                "smaller maximum on to the server, so the multi-message path "
                "was not exercised"
            )
        ids = [m["message_parameter"] for m in pieces]
        assert len(set(ids)) == len(ids), "suite error: the server reused an id"
        assert len(reply) == size, (
            f"a {size}-byte reply in {len(pieces)} messages "
            f"({ids[0]:#010x}..{ids[-1]:#010x}) came back as {len(reply)} bytes; "
            f"{client_says(inst)}"
        )
        return f"{size}B across {len(pieces)} messages"


# -------------------------------------------- overlapped-mode behaviour

@check("in overlapped mode pipelined queries are answered in order",
       rule="IVI-6.1 §3")
def check_pipelined_queries():
    """§3: "All HiSLIP clients shall support both synchronized and overlapped
    mode." What overlap mode is, §3 gives by example: "a series of independent query messages can be sent to the server
    without regard to when they complete. The responses from each will be
    returned in the order the queries were sent."

    In synchronized mode the same sequence is an interrupted error; here it is
    the point of the mode.
    """
    with overlapped_session() as (_srv, inst):
        idn, opc = idn_and_opc(inst)
        sequence = ["*IDN?", "*OPC?"] * 4
        for q in sequence:
            inst.write(q)
        got = [a_reply(inst) for _ in sequence]
        expected = [idn if q == "*IDN?" else opc for q in sequence]
        assert got == expected, (
            f"{len(sequence)} pipelined queries came back as {got!r}; "
            f"{client_says(inst)}"
        )
        return f"{len(sequence)} replies in order"


@check("in overlapped mode sending does not discard a buffered reply",
       rule="IVI-6.1 §3")
def check_send_keeps_buffered_reply():
    """§3: "All HiSLIP clients shall support both synchronized and overlapped
    mode." §3.2, describing overlapped mode, which has no "shall" of its own:
    "Buffers are only cleared by a device clear." The contrast is §3.1.2 rule
    3, where a synchronized client *must* clear buffered replies
    when it sends. A client applying that rule in overlapped mode throws away
    the first reply as soon as the second query goes out.

    The wait makes sure the first reply has reached the client before the
    second query is sent, so it is genuinely buffered rather than in flight.
    """
    with overlapped_session() as (srv, inst):
        idn, opc = idn_and_opc(inst)
        if srv is not None:
            srv.reset()
        inst.write("*IDN?")
        wait_for_replies(srv, 1)
        inst.write("*OPC?")
        first = a_reply(inst)
        second = a_reply(inst)
        assert (first, second) == (idn, opc), (
            f"after sending a second query with the first reply buffered, the "
            f"reads returned {first!r}, {second!r}; {client_says(inst)}"
        )
        return "both replies delivered"


@check("in overlapped mode a part-read reply does not lose the one behind it",
       rule="IVI-6.1 §3")
def check_partial_read_keeps_next_reply():
    """§3's requirement to support overlapped mode, and §3.2's description of
    its buffering, from the reading side: a caller reading a reply a few
    bytes at a time must get the rest of it, and then the next reply intact,
    though both may already be sitting in the client's buffers."""
    with overlapped_session() as (srv, inst):
        idn, opc = idn_and_opc(inst)
        if srv is not None:
            srv.reset()
        inst.write("*IDN?")
        inst.write("*OPC?")
        wait_for_replies(srv, 2)
        head = inst.read_bytes(4)
        rest = inst.read()
        nxt = a_reply(inst)
        assert (head.decode(errors="replace") + rest).strip() == idn, (
            f"the first reply read in two parts came back as "
            f"{head!r} + {rest!r}, expected {idn!r}"
        )
        assert nxt == opc, (
            f"the reply behind it came back as {nxt!r}, expected {opc!r}; "
            f"{client_says(inst)}"
        )
        return "first reply in two reads, second intact"


@check("in overlapped mode MAV tracks replies not yet read", rule="IVI-6.1 §3.2.2")
def check_overlapped_mav():
    """§6.14.2, a server rule: "If any messages have not been fully delivered
    to the client application, MAV shall be set true." The server computes it
    from the MessageID the client quotes, so what this checks end to end is the
    client's half, §3.2.2 item 1: quote wrong and MAV is wrong.

    §6.14.2 only says when MAV is true. That it is false once everything has
    been read is MAV's meaning in IEEE 488.2 ("message available"), not a
    sentence of IVI-6.1. And §6.14.2 binds the server: against real hardware a
    failure here may be the instrument's, which is why the messages below do
    not blame the client alone.

    Two queries, one read: a reply is still undelivered, so MAV is set --
    whether or not that reply already sits in the client's buffer, since
    "delivered" means to the application. Then read it, and MAV clears.
    """
    with overlapped_session() as (srv, inst):
        idn, opc = idn_and_opc(inst)
        if srv is not None:
            srv.reset()
        inst.write("*IDN?")
        inst.write("*OPC?")
        wait_for_replies(srv, 2)
        assert a_reply(inst) == idn, "the first reply was not *IDN?'s"
        pending = inst.read_stb()
        assert a_reply(inst) == opc, "the second reply was not *OPC?'s"
        drained = inst.read_stb()
        assert pending & STB_MAV, (
            f"with one reply unread the status byte was {pending:#04x}, MAV "
            f"clear -- the client quoted the wrong MessageID, or the server "
            f"computed MAV wrongly; {client_says(inst)}"
        )
        assert not drained & STB_MAV, (
            f"with every reply read the status byte was {drained:#04x}, MAV "
            f"still set -- the client quoted the wrong MessageID, or the "
            f"server computed MAV wrongly; {client_says(inst)}"
        )
        return f"{pending:#04x} with a reply pending, {drained:#04x} after"


@check("in overlapped mode a device clear discards replies not yet read",
       rule="IVI-6.1 §6.12")
def check_clear_discards_buffered_replies():
    """§6.12, the client's device-clear procedure, step 4: "Clear messages on
    the synchronous and asynchronous channels with the exception of FatalError,
    DeviceClearAcknowledge, and AsyncDeviceClearAcknowledge." The procedure is
    worded "the client will", not "shall". §3.2 agrees: "Buffers are only
    cleared by a device clear."

    Not VPP-4.3 RULE 5.1.8: that is about the formatted I/O buffer viScanf()
    keeps, and these checks read with viRead(). Replies left over from before it
    must not answer a query made after it."""
    with overlapped_session() as (srv, inst):
        idn, _opc = idn_and_opc(inst)
        if srv is not None:
            srv.reset()
        for _ in range(3):
            inst.write("*OPC?")
        wait_for_replies(srv, 3)
        inst.clear()
        got = inst.query("*IDN?").strip()
        assert got == idn, (
            f"the first query after a device clear returned {got!r}, a reply "
            f"left over from before it; {client_says(inst)}"
        )
        return "stale replies gone"


@check("in overlapped mode a reply abandoned part-way leaves nothing behind a clear",
       rule="IVI-6.1 §6.12")
def check_clear_mid_reply_leaves_no_residue():
    """The overlapped-mode hazard in 07_clear.py's mid-message clear. A device
    clear has the client discard the unread rest of the reply -- §6.12, client
    step 4, worded "the client will" (see the check above). In
    synchronized mode a client that fails to still has a safety net: the
    residue carries the old request's MessageID and §3.1.2 rule 1 throws it
    away. Overlapped mode has no such net (§3.2), so the residue is handed to
    the caller as the answer to whatever it asks next.

    The query after the clear is a *different* one on purpose. Repeating the
    large query would hide the lag: a session answering one behind returns
    the previous identical reply, which compares equal.
    """
    big = CTX["args"].big_query or ("TEST:BIG?" if CTX.get("server") else None)
    if not big:
        raise Skip("needs a large-reply query; name one with --big-query")
    with overlapped_session() as (srv, inst):
        if srv is not None:
            srv.big_reply(3000)
        idn = inst.query("*IDN?").strip()
        lib, sess = inst.visalib, inst.session
        lib.write(sess, big.encode() + b"\n")
        partial, _ = visa.call(lib.read, sess, 100)
        assert partial is not None and len(partial) == 100, (
            f"the partial read got {0 if partial is None else len(partial)} bytes"
        )
        # A clear that raises is 07_clear.py's finding, not this one's. What
        # matters here is what the session does next, so note it and go on.
        raised = ""
        try:
            inst.clear()
        except Exception as exc:  # noqa: BLE001
            raised = f" (viClear itself raised {type(exc).__name__}: {exc})"
        got = [inst.query("*IDN?").strip() for _ in range(3)]
        assert got == [idn] * 3, (
            f"after clearing part-way through a reply, *IDN? returned "
            f"{[g[:30] for g in got]!r}: what was left of the abandoned reply "
            f"was delivered as an answer to a later query{raised}; "
            f"{client_says(inst)}"
        )
        return "the abandoned reply was discarded" + raised


if __name__ == "__main__":
    script.run()
