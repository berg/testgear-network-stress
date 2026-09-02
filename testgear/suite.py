# SPDX-License-Identifier: GPL-3.0-or-later
"""The suite: which scripts run, in what order, with what arguments.

There used to be three copies of this list -- one unrolled in `run_all.sh`, one
in `run_all.py`, one in `compare.py` -- and they drifted. Commit 6581660 ("Fix
three bugs that deleted whole scripts from the vendor columns") is what that
drift looks like from the outside: two scripts died against the vendors and the
matrix showed blank cells, which read as "not applicable" rather than "this run
crashed".

One list, three consumers.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Script:
    """One check script, and what the runners need to know about it."""

    name: str

    #: Extra arguments, with `{iter}` and `{soak}` filled in by the runner.
    args: tuple[str, ...] = ()

    #: The one transport whose wire format this script inspects directly, if
    #: any. `None` means it is meaningful on both.
    only: str | None = None

    #: Whether the script belongs in a cross-backend matrix. The soak is left
    #: out on purpose: it is a randomised workload whose value is duration, and
    #: its single summary check says nothing in a matrix.
    in_matrix: bool = True

    def protocols(self, wanted: tuple[str, ...]) -> tuple[str, ...]:
        """Which of `wanted` this script should actually be run against."""
        if self.only is None:
            return wanted
        return (self.only,) if self.only in wanted else ()

    def argv(self, *, iterations: int, soak: int) -> list[str]:
        return [a.format(iter=iterations, soak=soak) for a in self.args]


#: Every script, in run order. The spec-conformance scripts are where nearly
#: every finding lives, so a list that quietly omits them reads as coverage
#: while covering a third of the suite.
SCRIPTS: tuple[Script, ...] = (
    Script("01_smoke.py"),
    Script("02_io.py", ("-n", "{iter}")),
    Script("03_srq.py", ("-n", "30")),
    Script("04_concurrency.py", ("-n", "{iter}")),
    Script("05_lock.py", ("-n", "{iter}")),
    Script("06_terminate.py", ("-n", "15")),
    Script("07_clear.py", ("-n", "40")),
    Script("09_remote_local.py"),
    Script("10_lock_semantics.py"),
    Script("12_session_lifecycle.py"),
    Script("13_events.py"),
    Script("15_required_attributes.py"),
    Script("16_operations.py"),
    Script("17_resource_names.py"),
    Script("conformance.py"),
    Script("08_soak.py", ("--duration", "{soak}", "--srq-thread"), in_matrix=False),
    Script("vxi11_conformance.py", only="vxi11"),
    Script("14_vxi11_flags.py", only="vxi11"),
    Script("11_hislip_messages.py", only="hislip"),
)

BY_NAME: dict[str, Script] = {s.name: s for s in SCRIPTS}


def for_protocol(protocol: str, *, matrix_only: bool = False) -> tuple[Script, ...]:
    """The scripts to run against one transport, in order."""
    return tuple(
        s
        for s in SCRIPTS
        if (not matrix_only or s.in_matrix) and s.protocols((protocol,))
    )


def names(protocol: str, *, matrix_only: bool = False) -> list[str]:
    return [s.name for s in for_protocol(protocol, matrix_only=matrix_only)]
