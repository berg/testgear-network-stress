# SPDX-License-Identifier: GPL-3.0-or-later
"""How a check script is invoked, in one place.

Every file in `checks/` ended with the same twenty-five lines: build the
parser, open the target, fill in the module's CTX, make a `Stats`, collect the
registered checks, run them, write the outputs, return the count. Nineteen
copies, differing in a title string and -- in two files -- a watchdog timeout.
That is the shape `testgear/suite.py` was written about one directory over:
the copies do not drift because anyone changes them carelessly, they drift
because a fix lands in the file someone happened to have open.

**Why a check script is still a process.** The obvious next step from here is
to stop running each file as its own program and drive them from one, which
would make this module unnecessary. It would also break the suite, for three
reasons worth writing down before someone tries it:

- `--pyvisa-py` swaps the `pyvisa_py` module tree, and it has to happen before
  anything imports it. `compare.py` compares two checkouts of pyvisa-py by
  running the same script twice under two interpreters; in one process there
  is one `sys.modules`, and the second column would silently be the first.
- The watchdog abandons threads on purpose (see `harness._with_watchdog`): a
  check that hangs leaves a daemon thread blocked in a syscall that is not
  going to return, holding a socket and often still driving the target.
  Nothing reaps those but process exit. One long-lived process accumulates
  them for the length of the sweep.
- Exit codes are the signal `run_all.py` and `tools/ci_status.py` both read.
  3 is the target going away, 5 is this suite calling VISA wrongly, and they
  have to be *per script* -- one column losing its instrument is not the same
  event as the sweep being broken.

So the process boundary stays, and the boilerplate that came with it lives
here instead of nineteen times.
"""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Callable

from . import cli, harness, suite


#: `only` is not a parameter with a default -- it is a parameter whose default
#: is "ask `suite.py`". A sentinel rather than None, because None is a
#: meaningful answer there (a script that runs on both transports) and has to
#: be distinguishable from "nobody said".
_FROM_SUITE = object()


def title_for(path) -> str:
    """"checks/12_session_lifecycle.py" -> "session lifecycle".

    The leading number is run order, which `suite.py` owns and a report has no
    use for. Everything else is already the title, because the files were
    named after what they check.
    """
    return re.sub(r"^\d+_", "", pathlib.Path(path).stem).replace("_", " ")


def run(
    *,
    title: str | None = None,
    only: str | None = _FROM_SUITE,
    watchdog: float | None = 30.0,
    on_timeout: Callable[[], None] | None = None,
) -> None:
    """Run every registered check in the calling module, and exit.

        if __name__ == "__main__":
            script.run()

    Nothing is passed in that can be looked up. The module is the caller's,
    the title comes from its filename, and which transports it belongs to
    comes from `testgear.suite` -- which already had to know, because it is
    what `run_all.py` and `compare.py` read to build the sweep and the matrix.
    Restating it here would be a second copy of a fact whose first copy exists
    specifically to stop there being several.

    The keyword arguments are for the four scripts that genuinely differ, and
    reading them at a call site should be enough to see that they do:

        script.run(title="vxi11 operation flags")
        script.run(watchdog=20.0, on_timeout=restart_server)

    `on_timeout` is called after a check is abandoned, to replace a target the
    abandoned thread is still talking to; see `harness.run_checks`.
    """
    module = sys.modules[sys._getframe(1).f_globals["__name__"]]
    path = pathlib.Path(module.__file__)
    if only is _FROM_SUITE:
        registered = suite.BY_NAME.get(path.name)
        if registered is None:
            # Not fatal -- a check being written is not yet part of the suite,
            # and having to edit two files to run it once would be worse. But
            # said out loud every time, because a finished script missing from
            # SCRIPTS runs for whoever invokes it directly and for nobody
            # else: not in the sweep, not in the matrix, not in CI. That is
            # the failure 6581660 is named after, arriving quietly.
            print(
                f"note: {path.name} is not in testgear.suite.SCRIPTS, so it "
                f"runs only when invoked directly -- not in run_all.py, "
                f"compare.py or CI",
                file=sys.stderr,
            )
        only = registered.only if registered else None

    harness.main(
        lambda: _run(
            module,
            title or title_for(path),
            only=only,
            watchdog=watchdog,
            on_timeout=on_timeout,
        )
    )


def _run(module, title, *, only, watchdog, on_timeout) -> int:
    parser = cli.build_parser(module.__doc__.splitlines()[0], protocol=only)
    # A script with options of its own says so in one place, next to the
    # checks that read them, rather than in a `main()` that exists only to
    # hold the parser. `cli.build_parser` still owns everything shared, so
    # `--protocol` cannot come to mean two things in two files.
    add_arguments = getattr(module, "add_arguments", None)
    if add_arguments is not None:
        add_arguments(parser)
    args = parser.parse_args()
    if only is not None and args.protocol != only:
        # Exit 4, the same code a missing backend uses: this is a script being
        # asked for something it does not do, not a check failing.
        print(f"this suite is {only.upper()} only", file=sys.stderr)
        return 4

    with cli.open_target(args) as (backend, resource, srv):
        # The checks reach their target through the module's own CTX rather
        # than through arguments, because `harness.run_checks` calls them with
        # no arguments -- a registered check is a nullary function so that its
        # name and its source location are the whole of its interface.
        stats = harness.Stats(
            title if only else f"{title} ({args.protocol})",
            verbose=args.verbose,
            context=cli.context(args, backend, resource),
        )
        ctx = getattr(module, "CTX", None)
        if ctx is not None:
            ctx.update(
                backend=backend,
                resource=resource,
                server=srv,
                timeout=args.timeout,
                protocol=args.protocol,
                args=args,
                # The live Stats, so a check can record a `note` -- an
                # observation that is not a pass or a fail. `visa.check_errors`
                # wants one too: a drained error queue is mostly notes, and
                # occasionally a desync, which is a finding with no check of
                # its own to hang from.
                stats=stats,
            )
        setup = getattr(module, "SETUP", None)
        try:
            if setup is not None:
                # A script whose checks share one session opens it here, so
                # the session is torn down even when a check leaves the
                # watchdog holding it.
                with setup(ctx):
                    harness.run_checks(
                        harness.collect(module, protocol=args.protocol),
                        stats,
                        watchdog=watchdog,
                        on_timeout=on_timeout,
                    )
            else:
                harness.run_checks(
                    harness.collect(module, protocol=args.protocol),
                    stats,
                    watchdog=watchdog,
                    on_timeout=on_timeout,
                )
        finally:
            # Written even if the teardown raises. A run that produced results
            # and then fell over on the way out still has something to say,
            # and `compare.py` treats a missing report as a column that never
            # ran at all.
            stats.write_outputs(args)
        return stats.finish()
