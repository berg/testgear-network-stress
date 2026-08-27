#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""IVI-6.1 section 3.1.2: what a HiSLIP client SHALL do with MessageIDs.

None of this is visible through the VISA API. The MessageID a client puts on a
message, and what it does when the server sends one back that does not match,
exist only on the wire -- so these checks read the proxy's message log and
inject skewed IDs into server replies.

The requirement is a desynchronisation defence. A DataEND carrying the wrong
MessageID means the client and server disagree about which request is being
answered, and the spec's answer is to throw the message away and clear anything
buffered rather than hand a caller data belonging to a different query. A client
that returns it instead is doing the single worst thing available: quietly
answering the wrong question.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testgear import cli, harness, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

# IVI-6.1 Table 4.
MSG_DATA, MSG_DATA_END, MSG_TRIGGER = 6, 7, 12
#: IVI-6.1 3.1.2: the counter a client starts from, and its step.
INITIAL_MESSAGE_ID = 0xFFFFFF00
MESSAGE_ID_STEP = 2

CTX: dict = {}


def open_inst(**kwargs):
    return visa.session(
        CTX["backend"], CTX["resource"], timeout=CTX["timeout"], **kwargs
    )


def server():
    if CTX.get("server") is None:
        raise Skip("needs the mock server: these are wire-level requirements")
    return CTX["server"]


def client_ids(srv) -> list[int]:
    """MessageIDs the client put on Data, DataEND and Trigger, in order."""
    return [
        m["message_parameter"]
        for m in srv.hislip_messages()
        if m["from"] == "client"
        and m["message_type"] in (MSG_DATA, MSG_DATA_END, MSG_TRIGGER)
    ]


@check("the client's MessageID starts at 0xFFFFFF00", rule="IVI-6.1 3.1.2")
def check_initial_message_id():
    """3.1.2: "Clients shall maintain a MessageID count that is initially set
    to 0xffff ff00"."""
    srv = server()
    with open_inst() as inst:
        srv.reset()
        inst.query("*IDN?")
        ids = client_ids(srv)
        assert ids, "the client sent no Data, DataEND or Trigger message"
        assert ids[0] == INITIAL_MESSAGE_ID, (
            f"the first MessageID was {ids[0]:#010x}, expected "
            f"{INITIAL_MESSAGE_ID:#010x}"
        )
        return f"{ids[0]:#010x}"


@check("the MessageID advances by two per message", rule="IVI-6.1 3.1.2")
def check_message_id_step():
    """3.1.2: incremented "by two in an unsigned 32-bit sense (permitting
    wrap-around)".

    Two, not one: the low bit is reserved, and a client stepping by one would
    eventually collide with the reserved 0xFFFFFFFF that rule 2 treats
    specially.
    """
    srv = server()
    with open_inst() as inst:
        srv.reset()
        for _ in range(4):
            inst.query("*IDN?")
        ids = client_ids(srv)
        assert len(ids) >= 4, f"expected at least 4 messages, saw {len(ids)}"
        steps = [
            (b - a) & 0xFFFFFFFF for a, b in zip(ids, ids[1:])
        ]
        assert all(s == MESSAGE_ID_STEP for s in steps), (
            f"MessageIDs stepped by {steps}, expected "
            f"{MESSAGE_ID_STEP} each time: {[f'{i:#010x}' for i in ids]}"
        )
        return f"{ids[0]:#010x} then +{MESSAGE_ID_STEP} x {len(steps)}"


@check("a DataEND with the wrong MessageID is discarded", rule="IVI-6.1 3.1.2")
def check_data_end_id_mismatch():
    """3.1.2 rule 1: verify the MessageID on a DataEND against the one last
    sent; if they differ, clear buffered Data responses and discard it.

    The failure this prevents is handing a caller the answer to a different
    question. Returning the payload is worse than any error, because nothing
    downstream can tell it is wrong.
    """
    srv = server()
    with open_inst() as inst:
        inst.timeout = 2000
        srv.reset()
        inst.query("*IDN?")  # establish a known-good exchange first

        with srv.hislip_faults(skew_data_end_id=4):
            try:
                reply = inst.query("*IDN?")
            except Exception as exc:  # noqa: BLE001
                # Discarding the message leaves nothing to return, so a
                # timeout or an I/O error is the correct outcome.
                return f"discarded, reported as {visa.visa_status(exc)}"
        raise AssertionError(
            f"a DataEND carrying a MessageID four higher than the request's "
            f"was accepted and its payload returned as {reply.strip()!r}. "
            f"3.1.2 rule 1 requires it to be discarded: the client and server "
            f"disagree about which request is being answered"
        )


@check("a Data message with the wrong MessageID is discarded",
       rule="IVI-6.1 3.1.2")
def check_data_id_mismatch():
    """3.1.2 rule 2, the same requirement for a non-final Data message.

    Whether this can run at all depends on the *server*: a server that sends
    every reply as a single DataEND, however large, never produces a Data
    message for the fault to land on. The first version of this check did not
    look, armed a fault that could never fire, and reported the resulting
    success as a client failure -- a check bug wearing a finding's clothes.
    So it establishes that a Data message actually occurs before claiming
    anything about what the client did with one.
    """
    srv = server()
    srv.big_reply(400_000)
    with open_inst() as inst:
        inst.timeout = 3000
        srv.reset()
        inst.query("TEST:BIG?")
        kinds = {
            m["message_type"] for m in srv.hislip_messages() if m["from"] == "server"
        }
        if MSG_DATA not in kinds:
            raise Skip(
                "this server sends every reply as a single DataEND, even at "
                "400 kB, so there is no Data message to mis-address. Rule 2 "
                "needs a server that chunks its replies"
            )

        srv.reset()
        with srv.hislip_faults(skew_data_id=6):
            try:
                reply = inst.query("TEST:BIG?")
            except Exception as exc:  # noqa: BLE001
                return f"discarded, reported as {visa.visa_status(exc)}"
        raise AssertionError(
            f"a Data message with a mismatched MessageID was accepted; "
            f"{len(reply)} bytes were returned"
        )


@check("the session recovers after a discarded message", rule="IVI-6.1 3.1.2")
def check_recovery_after_mismatch():
    """Discarding is only half of rule 1; the session has to remain usable.

    A client that discards the message but leaves its buffers holding the
    payload answers the *next* query with the previous one's data, which is
    the same silent failure one exchange later.
    """
    srv = server()
    with open_inst() as inst:
        inst.timeout = 2000
        srv.reset()
        idn = inst.query("*IDN?").strip()
        with srv.hislip_faults(skew_data_end_id=8):
            try:
                inst.query("*IDN?")
            except Exception:  # noqa: BLE001
                pass
        try:
            inst.clear()
        except Exception:  # noqa: BLE001
            pass
        after = inst.query("*IDN?").strip()
        assert after == idn, (
            f"after a mismatched MessageID the next query returned {after!r}, "
            f"expected {idn!r}: the buffered response was not cleared"
        )


@check("the MessageID resets to 0xFFFFFF00 after a device clear",
       rule="IVI-6.1 3.1.2")
def check_message_id_reset_on_clear():
    """3.1.2, and step 8 of the device-clear procedure: "The MessageID is
    reset to 0xffff ff00 after device clear, and when the connection is
    initialized."

    Both ends reset, so a client that keeps counting is immediately out of
    step with a server that started again -- and every subsequent DataEND
    then fails the rule-1 check the client itself is supposed to apply. The
    symptom is not a wrong answer but a session that stops working after its
    first clear.
    """
    srv = server()
    with open_inst() as inst:
        inst.timeout = 3000
        srv.reset()
        for _ in range(3):
            inst.query("*IDN?")
        before = client_ids(srv)
        assert len(before) >= 3, f"expected 3 messages, saw {len(before)}"
        assert before[-1] != INITIAL_MESSAGE_ID, (
            "the counter had not advanced, so a reset cannot be observed"
        )

        inst.clear()
        srv.reset()
        inst.query("*IDN?")
        after = client_ids(srv)
        assert after, "no message followed the device clear"
        assert after[0] == INITIAL_MESSAGE_ID, (
            f"the first MessageID after a device clear was {after[0]:#010x}, "
            f"expected {INITIAL_MESSAGE_ID:#010x}. The server resets too, so a "
            f"client that keeps counting is out of step from here on"
        )
        return f"{before[-1]:#010x} -> clear -> {after[0]:#010x}"


@check("the client reports whether overlap mode is in use", rule="IVI-6.1 2.7")
def check_overlap_mode_attribute():
    """2.7: "All HiSLIP clients shall support both synchronized and overlapped
    mode."

    Which mode a session is in changes what the client is allowed to do with
    MessageIDs, so a caller has to be able to find out. VPP-4.3 5.1.17 makes
    VI_ATTR_TCPIP_HISLIP_OVERLAP_EN required for exactly that reason.
    """
    from pyvisa.constants import ResourceAttribute as RA

    with open_inst() as inst:
        value, st = visa.call(
            inst.visalib.get_attribute, inst.session, RA.tcpip_hislip_overlap_enable
        )
        from pyvisa.constants import StatusCode

        assert st == StatusCode.success and value is not None, (
            f"VI_ATTR_TCPIP_HISLIP_OVERLAP_EN is not readable ({st!r}), so a "
            f"caller cannot tell which mode the session negotiated"
        )
        return f"overlap_en={value!r}"


@check("every client message carries the HS prologue", rule="IVI-6.1 2.3")
def check_prologue():
    """2.3: the prologue "shall be ASCII 'HS'".

    Checked by parsing: the proxy only recognises a header at all when the
    prologue is right, so a client emitting anything else would produce no
    observed messages.
    """
    srv = server()
    with open_inst() as inst:
        srv.reset()
        inst.query("*IDN?")
        messages = srv.hislip_messages()
        client = [m for m in messages if m["from"] == "client"]
        assert client, (
            "no client message was recognised, which means the prologue was "
            "not ASCII 'HS' -- the parser keys on it"
        )
        return f"{len(client)} client messages, all with a valid prologue"


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0], protocol="hislip")
    args = parser.parse_args()
    if args.protocol != "hislip":
        print("this suite is HiSLIP only", file=sys.stderr)
        return 4

    with cli.open_target(args) as (backend, resource, srv):
        CTX.update(
            backend=backend,
            resource=resource,
            server=srv,
            timeout=args.timeout,
            protocol=args.protocol,
        )
        stats = harness.Stats(
            "hislip messages",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        checks = harness.collect(sys.modules[__name__], protocol="hislip")
        harness.run_checks(checks, stats, watchdog=30.0)
        stats.write_outputs(args)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
