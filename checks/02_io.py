#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pound on the read/write path and verify every byte that comes back.

Every response is checked against what the instrument returned the first time,
so a desynchronised message stream shows up as a mismatch rather than as
silent corruption. Checking only that calls do not raise would miss exactly
the bugs this path has.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import harness, script, visa  # noqa: E402
from testgear.harness import Skip, check  # noqa: E402

CTX: dict = {}
STATE: dict = {}

READ_OK = (
    StatusCode.success,
    StatusCode.success_max_count_read,
    StatusCode.success_termination_character_read,
)

#: Read sizes for the chunked-read section. 1 and 7 are deliberately not
#: factors of anything; 997 is prime and larger than most transport frames.
CHUNKS = (1, 7, 64, 997)

#: Reading 3 kB one byte at a time is 3201 round trips: under a second over
#: HiSLIP, fifty over VXI-11 where every viRead is its own RPC. The default
#: watchdog would abandon it mid-message and the abandoned thread would go on
#: draining the stream into the next check's read.
CHUNK_WATCHDOG = 180.0

#: A response with interior newlines, so a termchar read has something to stop
#: on before the message ends.
MULTILINE = "line-one\nline-two\nline-three\n"


def add_arguments(parser) -> None:
    parser.add_argument(
        "--min-query-rate",
        type=float,
        default=0.0,
        help="fail if the query storm runs slower than this many per second. "
        "Off by default: a sensible floor is instrument specific -- this suite "
        "sees thousands/s from the mock and 3/s from a DMM integrating at "
        "NPLC 10 -- so set it per instrument in CI to catch regressions like "
        "an instrument left in local mode",
    )


def io():
    return CTX["session"].visalib, CTX["session"].session


@contextlib.contextmanager
def SETUP(ctx):
    with visa.session(
        ctx["backend"], ctx["resource"], timeout=ctx["timeout"]
    ) as session:
        ctx["session"] = session
        STATE["idn"] = session.query("*IDN?").strip()
        visa.drain_errors(session)
        ctx["stats"].note(f"instrument: {STATE['idn']}")
        STATE["big_query"] = visa.resolve_big_query(
            ctx["args"], ctx["server"], session, ctx["stats"]
        )
        try:
            yield
        finally:
            visa.check_errors(session, ctx["stats"], "at end of run")


def big_query() -> str:
    """The large-response query, or skip saying there is not one."""
    if not STATE["big_query"]:
        raise Skip("there is no usable large-response query on this instrument")
    return STATE["big_query"]


def reference() -> str:
    """The large response, read once and remembered."""
    if "reference" not in STATE:
        STATE["reference"] = CTX["session"].query(big_query())
        CTX["stats"].note(
            f"{STATE['big_query']} returns {len(STATE['reference'])} bytes"
        )
    return STATE["reference"]


# -- 1. small query storm ----------------------------------------------------
@check("a storm of small queries all answer correctly")
def check_small_query_storm():
    args, stats = CTX["args"], CTX["stats"]
    t0 = time.time()
    for i in range(args.iterations):
        got = CTX["session"].query("*IDN?").strip()
        assert got == STATE["idn"], (
            f"iteration {i} returned {got!r}, expected {STATE['idn']!r}"
        )
    elapsed = time.time() - t0
    STATE["rate"] = args.iterations / elapsed
    stats.note(
        f"{args.iterations} queries in {elapsed:.2f}s ({STATE['rate']:.0f}/s)"
    )
    # Recorded here rather than as a check of its own, and only when asked
    # for. A sensible floor is instrument specific, so this is a regression
    # guard somebody configures per instrument -- and a check that exists in
    # every column saying "nobody set a floor" is a SKIP that means nothing.
    if args.min_query_rate:
        stats.check(
            STATE["rate"] >= args.min_query_rate,
            "the query rate meets the configured floor",
            detail=f"{STATE['rate']:.0f}/s against a floor of "
            f"{args.min_query_rate:.0f}/s",
        )
    return f"{args.iterations} queries in {elapsed:.2f}s ({STATE['rate']:.0f}/s)"


# -- 2. large responses ------------------------------------------------------
@check("a storm of large-response queries all answer correctly")
def check_large_query_storm():
    args, stats = CTX["args"], CTX["stats"]
    expected = reference()
    iterations = max(1, args.iterations // 10)
    t0 = time.time()
    for i in range(iterations):
        got = CTX["session"].query(big_query())
        assert got == expected, (
            f"read {i} got {len(got)} bytes, expected {len(expected)}"
        )
    elapsed = time.time() - t0
    throughput = iterations * len(expected) / elapsed / 1024
    stats.note(
        f"{iterations} x {len(expected)}B in {elapsed:.2f}s "
        f"({throughput:.0f} kB/s)"
    )
    return (
        f"{iterations} x {len(expected)}B in {elapsed:.2f}s "
        f"({throughput:.0f} KiB/s)"
    )


# -- 3. reading a message in small pieces ------------------------------------
# This walks the payload-remaining state machine across many viRead calls for a
# single transport message, which is where the end-of-message-lost-to-the-
# byte-count bug lived.
def _read_in_chunks(chunk: int):
    """Read one whole large response `chunk` bytes at a time.

    Remembered per chunk size, because the intact check and the final-status
    check are two rows about one read and re-reading for each would double the
    work and let them disagree.
    """
    key = f"chunked_{chunk}"
    if key not in STATE:
        expected = reference()
        lib, sess = io()
        lib.write(sess, big_query().encode() + b"\n")
        pieces: list[bytes] = []
        statuses: list = []
        while sum(len(p) for p in pieces) < len(expected):
            data, st = visa.call(lib.read, sess, chunk)
            statuses.append(st)
            if st not in READ_OK or not data:
                break
            pieces.append(data)
        STATE[key] = (b"".join(pieces).decode("latin-1"), statuses)
    return STATE[key]


def _chunk_intact(chunk: int):
    def run():
        joined, _ = _read_in_chunks(chunk)
        expected = reference()
        detail = f"{len(expected)}B expected, {len(joined)}B read"
        assert joined == expected, detail
        return detail

    return run


def _chunk_final_status(chunk: int):
    def run():
        _, statuses = _read_in_chunks(chunk)
        detail = f"got {statuses[-1] if statuses else 'nothing'!r}"
        assert statuses and statuses[-1] == StatusCode.success, detail
        return detail

    return run


def _register_chunk_checks() -> None:
    add = harness.registrar(globals())
    for chunk in CHUNKS:
        add(
            _chunk_intact(chunk),
            f"a large message read {chunk}B at a time is intact",
            rule="VPP-4.3 RULE 6.1.2",
            watchdog=CHUNK_WATCHDOG,
        )
        add(
            _chunk_final_status(chunk),
            f"the final {chunk}B chunk reports VI_SUCCESS",
            rule="VPP-4.3 RULE 6.1.1",
            watchdog=CHUNK_WATCHDOG,
        )


_register_chunk_checks()


# -- 4. termination character handling ---------------------------------------
@check("repeated termination-character reads all succeed", rule="VPP-4.3 RULE 6.1.1")
def check_termchar_reads():
    """VI_SUCCESS *or* VI_SUCCESS_TERM_CHAR is correct here, and which one
    depends on the instrument rather than on the client.

    RULE 6.1.1 gives the END indicator priority, so a reply whose last byte
    carries END reports plain success even though it also happens to end in
    the termchar. Only a reply that stopped on the termchar *without* END is
    VI_SUCCESS_TERM_CHAR. Demanding the latter unconditionally fails every
    instrument that asserts END, which is most of them.
    """
    inst, (lib, sess) = CTX["session"], io()
    inst.set_visa_attribute(RA.termchar, ord("\n"))
    inst.set_visa_attribute(RA.termchar_enabled, True)
    st = None
    try:
        for i in range(min(CTX["args"].iterations, 50)):
            lib.write(sess, b"*IDN?\n")
            data, st = visa.call(lib.read, sess, 4096)
            assert st in (
                StatusCode.success,
                StatusCode.success_termination_character_read,
            ), f"read {i} got {st!r}"
            assert data.strip() == STATE["idn"].encode(), f"read {i} got {data!r}"
            assert data.endswith(b"\n"), (
                f"read {i} did not stop on the termination character"
            )
    finally:
        inst.set_visa_attribute(RA.termchar_enabled, False)
    return f"last status {st!r}"


def _multiline_read():
    """Read a multi-line response back, one termchar-stopped read at a time.

    Both checks below are about the same read, so it happens once.
    """
    if "multiline" not in STATE:
        inst, (lib, sess) = CTX["session"], io()
        server = CTX["server"]
        if server is not None:
            server.respond("TEST:LINES?", MULTILINE.rstrip("\n"))
            probe, expected = "TEST:LINES?", MULTILINE
        else:
            expected = reference()
            if "\n" not in expected[:-1]:
                raise Skip(
                    "no multi-line response is available from this instrument"
                )
            probe = big_query()
        inst.set_visa_attribute(RA.termchar_enabled, True)
        try:
            lib.write(sess, probe.encode() + b"\n")
            collected: list[bytes] = []
            while sum(len(c) for c in collected) < len(expected):
                data, st = visa.call(lib.read, sess, 65536)
                if not data:
                    break
                collected.append(data)
                if st == StatusCode.success:
                    break
        finally:
            inst.set_visa_attribute(RA.termchar_enabled, False)
        STATE["multiline"] = (b"".join(collected).decode("latin-1"), collected, expected)
    return STATE["multiline"]


@check("a multi-line response reassembles across termchar reads",
       rule="VPP-4.3 RULE 6.1.3")
def check_multiline_reassembles():
    joined, _, expected = _multiline_read()
    assert joined == expected, f"got {joined!r}"
    return f"got {joined!r}"


@check("the response was split on the termchar", rule="VPP-4.3 RULE 6.1.3")
def check_multiline_split():
    """A single read that swallowed the whole thing means the termchar was not
    honoured, even though the bytes came back correct."""
    _, collected, _ = _multiline_read()
    assert len(collected) > 1, f"{len(collected)} reads"
    return f"{len(collected)} reads"


# -- 5. send-end disabled ----------------------------------------------------
@check("a message split across an unterminated and a terminated write is "
       "reassembled by the instrument", rule="VPP-4.3 3.2.1")
def check_split_write():
    """With END suppressed the message goes out unterminated, so the
    instrument should not answer until a terminated message follows."""
    inst, (lib, sess) = CTX["session"], io()
    inst.set_visa_attribute(RA.send_end_enabled, False)
    try:
        lib.write(sess, b"*IDN")
        inst.set_visa_attribute(RA.send_end_enabled, True)
        lib.write(sess, b"?\n")
        data, _ = visa.call(lib.read, sess, 4096)
        assert data is not None and data.strip() == STATE["idn"].encode(), (
            f"got {data!r}"
        )
        return f"got {data!r}"
    finally:
        inst.set_visa_attribute(RA.send_end_enabled, True)


# -- 6. still healthy --------------------------------------------------------
@check("the session still works at the end")
def check_healthy_at_end():
    final = CTX["session"].query("*IDN?").strip()
    assert final == STATE["idn"], f"got {final!r}"
    return f"got {final!r}"


if __name__ == "__main__":
    script.run()
