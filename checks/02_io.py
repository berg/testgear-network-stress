#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pound on the read/write path and verify every byte that comes back.

Every response is checked against what the instrument returned the first time,
so a desynchronised message stream shows up as a mismatch rather than as
silent corruption. Checking only that calls do not raise would miss exactly
the bugs this path has.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyvisa.constants import ResourceAttribute as RA  # noqa: E402
from pyvisa.constants import StatusCode  # noqa: E402

from testgear import cli, harness, visa  # noqa: E402

READ_OK = (
    StatusCode.success,
    StatusCode.success_max_count_read,
    StatusCode.success_termination_character_read,
)


def main() -> int:
    parser = cli.build_parser(__doc__.splitlines()[0])
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
    args = parser.parse_args()

    with cli.open_target(args) as (backend, resource, srv):
        stats = harness.Stats(
            f"io ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        with visa.session(backend, resource, timeout=args.timeout) as inst:
            lib, sess = inst.visalib, inst.session

            idn = inst.query("*IDN?").strip()
            visa.drain_errors(inst)
            stats.note(f"instrument: {idn}")
            big_query = visa.resolve_big_query(args, srv, inst, stats)

            # -- 1. small query storm ---------------------------------------
            t0 = time.time()
            for i in range(args.iterations):
                got = inst.query("*IDN?").strip()
                if got != idn:
                    stats.error(
                        f"iteration {i}: *IDN? returned {got!r}, expected {idn!r}"
                    )
                    break
            else:
                elapsed = time.time() - t0
                stats.check(True, "small query storm")
                rate = args.iterations / elapsed
                stats.note(
                    f"{args.iterations} queries in {elapsed:.2f}s ({rate:.0f}/s)"
                )
                if args.min_query_rate:
                    stats.check(
                        rate >= args.min_query_rate,
                        f"query rate {rate:.0f}/s meets the floor of "
                        f"{args.min_query_rate:.0f}/s",
                    )

            # -- 2. large responses -----------------------------------------
            reference = None
            if big_query:
                reference = inst.query(big_query)
                stats.note(f"{big_query} returns {len(reference)} bytes")
                big_iterations = max(1, args.iterations // 10)
                t0 = time.time()
                for i in range(big_iterations):
                    got = inst.query(big_query)
                    if got != reference:
                        stats.error(
                            f"large read {i}: got {len(got)} bytes, "
                            f"expected {len(reference)}"
                        )
                        break
                else:
                    elapsed = time.time() - t0
                    stats.check(True, "large response storm")
                    throughput = big_iterations * len(reference) / elapsed / 1024
                    stats.note(
                        f"{big_iterations} x {len(reference)}B in {elapsed:.2f}s "
                        f"({throughput:.0f} kB/s)"
                    )

            # -- 3. reading a message in small pieces -------------------------
            # This walks the payload-remaining state machine across many
            # viRead calls for a single transport message, which is where the
            # end-of-message-lost-to-the-byte-count bug lived.
            if reference:
                for chunk in (1, 7, 64, 997):
                    lib.write(sess, big_query.encode() + b"\n")
                    pieces: list[bytes] = []
                    statuses: list[StatusCode] = []
                    while sum(len(p) for p in pieces) < len(reference):
                        data, st = visa.call(lib.read, sess, chunk)
                        statuses.append(st)
                        if st not in READ_OK:
                            stats.error(f"chunked read (chunk={chunk}) status {st!r}")
                            break
                        if not data:
                            stats.error(
                                f"chunked read (chunk={chunk}) returned no data"
                            )
                            break
                        pieces.append(data)
                    joined = b"".join(pieces).decode("latin-1")
                    stats.check(
                        joined == reference,
                        f"a {len(reference)}B message read {chunk}B at a time "
                        f"is intact",
                        rule="VPP-4.3 RULE 6.1.2",
                    )
                    stats.check(
                        statuses and statuses[-1] == StatusCode.success,
                        f"the final chunk (chunk={chunk}) reports VI_SUCCESS, "
                        f"got {statuses[-1] if statuses else 'nothing'!r}",
                        rule="VPP-4.3 RULE 6.1.1",
                    )

            # -- 4. termination character handling ----------------------------
            inst.set_visa_attribute(RA.termchar, ord("\n"))
            inst.set_visa_attribute(RA.termchar_enabled, True)
            try:
                for i in range(min(args.iterations, 50)):
                    lib.write(sess, b"*IDN?\n")
                    data, st = visa.call(lib.read, sess, 4096)
                    # VI_SUCCESS *or* VI_SUCCESS_TERM_CHAR is correct here, and
                    # which one depends on the instrument rather than on the
                    # client: RULE 6.1.1 gives the END indicator priority, so a
                    # reply whose last byte carries END reports plain success
                    # even though it also happens to end in the termchar. Only
                    # a reply that stopped on the termchar *without* END is
                    # VI_SUCCESS_TERM_CHAR. Demanding the latter unconditionally
                    # fails every instrument that asserts END, which is most of
                    # them.
                    if st not in (
                        StatusCode.success,
                        StatusCode.success_termination_character_read,
                    ):
                        stats.error(
                            f"termchar read {i}: expected VI_SUCCESS or "
                            f"VI_SUCCESS_TERM_CHAR, got {st!r}"
                        )
                        break
                    if data.strip() != idn.encode():
                        stats.error(f"termchar read {i}: got {data!r}")
                        break
                    if not data.endswith(b"\n"):
                        stats.error(
                            f"termchar read {i} did not stop on the termchar"
                        )
                        break
                else:
                    stats.check(
                        True,
                        f"termination character reads (last status {st!r})",
                        rule="VPP-4.3 RULE 6.1.1",
                    )
            finally:
                inst.set_visa_attribute(RA.termchar_enabled, False)

            # A read of a multi-line response with termchar on must hand back
            # the leading line and keep the rest for the next read.
            multiline = "line-one\nline-two\nline-three\n"
            if srv is not None:
                srv.respond("TEST:LINES?", multiline.rstrip("\n"))
            probe = "TEST:LINES?" if srv is not None else big_query
            have_multiline = srv is not None or (
                reference and "\n" in reference[:-1]
            )
            if have_multiline:
                expected = multiline if srv is not None else reference
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
                    stats.check(
                        b"".join(collected).decode("latin-1") == expected,
                        "a multi-line response reassembles across termchar reads",
                        rule="VPP-4.3 RULE 6.1.3",
                    )
                    stats.check(
                        len(collected) > 1,
                        f"the response was split on the termchar "
                        f"({len(collected)} reads)",
                        rule="VPP-4.3 RULE 6.1.3",
                    )
                finally:
                    inst.set_visa_attribute(RA.termchar_enabled, False)
            else:
                stats.skip(
                    "the multi-line termchar checks: no multi-line response "
                    "is available from this instrument"
                )

            # -- 5. send-end disabled ------------------------------------------
            # With END suppressed the message goes out unterminated, so the
            # instrument should not answer until a terminated message follows.
            inst.set_visa_attribute(RA.send_end_enabled, False)
            try:
                lib.write(sess, b"*IDN")
                inst.set_visa_attribute(RA.send_end_enabled, True)
                lib.write(sess, b"?\n")
                data, st = visa.call(lib.read, sess, 4096)
                stats.check(
                    data is not None and data.strip() == idn.encode(),
                    f"a message split across an unterminated and a terminated "
                    f"write is reassembled by the instrument, got {data!r}",
                    rule="VPP-4.3 3.2.1",
                )
            except Exception as exc:  # noqa: BLE001
                stats.error("split unterminated/terminated write failed", exc)
            finally:
                inst.set_visa_attribute(RA.send_end_enabled, True)

            # -- 6. still healthy -----------------------------------------------
            stats.check(
                inst.query("*IDN?").strip() == idn,
                "the session still works at the end",
            )
            visa.check_errors(inst, stats, "at end of run")

        if args.report:
            stats.write_report(args.report)
        return stats.finish()


if __name__ == "__main__":
    harness.main(main)
