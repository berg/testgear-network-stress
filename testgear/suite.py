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

    #: Arguments that come from a runner flag the caller may not have set,
    #: as (flag, keyword). The pair is emitted only when the runner was given
    #: a value, so the script's own `add_arguments` default stays the single
    #: definition of it rather than being shadowed by a second one here.
    optional: tuple[tuple[str, str], ...] = ()

    #: Seconds this script needs *on top of* the runner's per-script timeout,
    #: as a `{iter}`/`{soak}` template. Only the soak has one: its duration is
    #: the caller's choice, so no constant can be right for both `--soak 60`
    #: and an overnight run. Everything else is bounded by its own watchdogs
    #: and the runner's default is a backstop for the script wedging entirely.
    extra_budget: str = ""

    def protocols(self, wanted: tuple[str, ...]) -> tuple[str, ...]:
        """Which of `wanted` this script should actually be run against."""
        if self.only is None:
            return wanted
        return (self.only,) if self.only in wanted else ()

    def argv(self, *, iterations: int, soak: int, **optional) -> list[str]:
        """The arguments this script is run with.

        `optional` carries the runner flags that are not always set --
        `sessions=None` means "the caller did not ask", and the script's own
        default applies. Passing them through unconditionally would make this
        file a second place that decides how many parallel sessions is
        reasonable, and the two would drift.
        """
        out = [a.format(iter=iterations, soak=soak) for a in self.args]
        for flag, key in self.optional:
            value = optional.get(key)
            if value is not None:
                out += [flag, str(value)]
        return out

    def budget(self, *, default: float, iterations: int, soak: int) -> float:
        """Wall clock this script may take before the runner kills it."""
        if not self.extra_budget:
            return default
        return default + float(self.extra_budget.format(iter=iterations, soak=soak))


#: Every script, in run order. The spec-conformance scripts are where nearly
#: every finding lives, so a list that quietly omits them reads as coverage
#: while covering a third of the suite.
SCRIPTS: tuple[Script, ...] = (
    Script("01_smoke.py"),
    Script("02_io.py", ("-n", "{iter}")),
    Script("03_srq.py", ("-n", "30")),
    # --sessions opens that many connections to one instrument at once, and
    # some instruments cap them well below the default: a 34465A's HiSLIP
    # server stopped accepting connections entirely for the rest of the run
    # (issue #5). Reachable from the sweep so a target that cannot take six
    # can be told so without invoking this script by hand.
    Script(
        "04_concurrency.py",
        ("-n", "{iter}"),
        optional=(("--sessions", "sessions"),),
    ),
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
    Script(
        "08_soak.py",
        ("--duration", "{soak}", "--srq-thread"),
        in_matrix=False,
        extra_budget="{soak}",
    ),
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
