#!/usr/bin/env python3
"""Bench check for real GTL / LLO — the part only a human at the panel can see.

ugpibd used to approximate the remote/local control codes by driving REN alone.
It now sends an addressed GTL (0x01) and a universal LLO (0x11). The command
bytes are pinned by unit tests and have been read off the wire on both an NI
GPIB-USB-HS and an 82357B. What no measurement can settle is whether the
instrument obeys them, because the answer is on its front panel.

    ./.venv/bin/python bench_gtl.py -r TCPIP0::127.0.0.1::hislip23,4880::INSTR

How to read this script: each step sends **one** thing, then tells you what to
look at before you press Enter again. Nothing is sent behind your back — in
particular no queries, because addressing the instrument is itself what returns
it to remote and would undo the state you are being asked to observe.

Two things about a 34401A worth having straight first, or the results read as
nonsense:

  * In remote (RMT lit) the front-panel keys are already dead. All of them
    except LOCAL. So "the keys do nothing" proves nothing by itself.
  * LOCAL is therefore the only interesting key. Without a lockout it returns
    the instrument to local. Under LLO it stops working too — and that, not
    the other keys, is what LLO does.
"""
import sys

import common
from pyvisa import constants

REN = constants.RENLineOperation


def main():
    parser = common.build_parser(__doc__)
    args = parser.parse_args()
    rm, inst = common.open_session(args)
    lib, sess = inst.visalib, inst.session

    def ren(mode):
        st = common.status(lib.gpib_control_ren, sess, mode)
        if st != constants.StatusCode.success:
            print(f"        !! {mode.name} returned {st!r}")

    def look(expected):
        input(f"\n  LOOK AT THE PANEL >>> {expected}\n  [enter] to continue ")

    def step(n, what):
        input(f"\n=== {n}. {what}\n  [enter] to send ")

    print(f"\ninstrument: {inst.query('*IDN?').strip()}")
    print("(that query addressed it, so it is in remote now)")

    step(1, "assert REN and address the instrument — the normal remote state")
    ren(REN.asrt_address)
    look("RMT is LIT")

    step(2, "addressed GTL, with REN left asserted")
    ren(REN.address_gtl)
    look(
        "RMT has gone DARK.\n"
        "      This is the whole point: REN is still asserted, so any other\n"
        "      instrument on the bus stays in remote. Dropping REN, which is\n"
        "      what this used to do, would have taken them all local."
    )

    step(3, "one query, to show what re-addressing does")
    inst.query("*IDN?")
    look(
        "RMT is LIT again, by itself.\n"
        "      Correct, not a bug: addressing a device as a listener while REN\n"
        "      is asserted returns it to remote, so GTL only lasts until the\n"
        "      next write. Worth knowing before it gets reported as one."
    )

    step(4, "nothing — this step is yours")
    look(
        "press LOCAL on the front panel (Shift on a 34401A).\n"
        "      RMT should go DARK: no lockout is in force, so the key works.\n"
        "      If it does nothing even now, say so — the rest of this test\n"
        "      depends on LOCAL being live to begin with."
    )

    step(5, "back to remote, then local lockout")
    ren(REN.asrt_address)
    ren(REN.asrt_llo)
    look("RMT is LIT")

    step(6, "nothing — yours again, and this is the one that matters")
    look(
        "press LOCAL again.\n"
        "      Now it should do NOTHING and RMT should stay LIT.\n"
        "      That is LLO, and it is the one thing driving REN alone could\n"
        "      never do. If LOCAL still drops it to local, LLO did not reach\n"
        "      the instrument."
    )

    step(7, "drop REN")
    ren(REN.deassert)
    look(
        "RMT is DARK and LOCAL works again.\n"
        "      Dropping REN is the only way to clear a lockout — IEEE-488 has\n"
        "      no un-LLO command."
    )

    # Hand the bus back in remote, where a session should leave it.
    ren(REN.asrt_address)
    common.drain_errors(inst)
    print("\ndone — REN asserted, instrument addressed, error queue clear")
    inst.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
