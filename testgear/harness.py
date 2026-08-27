# SPDX-License-Identifier: GPL-3.0-or-later
"""PASS/FAIL/SKIP bookkeeping, shared by every check in the suite.

Two things here are load-bearing.

**A skip is not a pass.** Skips are counted separately, listed again in the
summary, and carried into the machine-readable report. The large-reply checks
in the original HiSLIP suite stayed skipped against a 34401A through the whole
of that suite's development without anyone noticing, because at a glance a
skipped check reads like a passing one. Anything that makes skips easy to
overlook is a bug in the harness.

**Results are structured, not just printed.** A check produces a named
`Result`, so the same run can be rendered as human output and as JSON. The
cross-backend comparison in `compare.py` is built entirely out of those names:
"check X passed on NI-VISA and failed on pyvisa-py" is only expressible if
both runs agree on what X is called.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import threading
import time
import traceback
from typing import Callable, Iterable

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class Skip(Exception):
    """Raised by a check that cannot run in this configuration."""


#: Parts of a check name that vary between runs without changing what the
#: check *is*: status-code reprs, measured numbers, quoted values, and the
#: bracketed lists of sample failures.
_VARIABLE = re.compile(
    r"""
      <[A-Za-z_]+\.[^>]*>          # <StatusCode.success: 0> and friends
    | \[[^\]]*\]                   # ['a', 'b'] sample lists
    | '[^']*' | "[^"]*"           # quoted values
    | \b\d+(?:\.\d+)?\b          # measurements and counts
    | \b(?:None|True|False)\b    # the value a backend returned, or did not
    """,
    re.VERBOSE,
)


def stable_key(name: str) -> str:
    """An identity for a check that survives its measurements changing.

    Check names here carry their evidence -- "the final chunk (chunk=64)
    reports VI_SUCCESS, got <StatusCode.success: 0>" -- which is exactly what
    you want when reading one run and exactly wrong when lining several up
    against each other: two backends reporting different status codes produce
    two different names, so the row splits in two and each column shows a gap
    where the other one answered.

    Masking the variable parts gives a key that matches across runs while the
    displayed name keeps its evidence. It is a heuristic, and the failure mode
    is benign in both directions: two genuinely different checks that mask to
    the same key would merge (they would have to be near-identically worded),
    and a name whose *wording* changes between versions splits, which is
    honest -- it is a different check.
    """
    return _VARIABLE.sub("*", name).strip()


@dataclasses.dataclass
class Result:
    name: str
    outcome: str
    detail: str = ""
    duration: float = 0.0
    #: The spec clause this check rests on, when it has one. A failure that
    #: cites a rule is a bug report; one that does not is an opinion.
    rule: str = ""

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["key"] = stable_key(self.name)
        return data


def check(name: str, rule: str = "", protocols: Iterable[str] = ("vxi11", "hislip")):
    """Register a function as a named check.

    `rule` names the clause the check enforces, so a failure message can cite
    it. `protocols` limits a check to the transports it makes sense for.
    """

    def wrap(func: Callable) -> Callable:
        func._check_name = name
        func._check_rule = rule
        func._check_protocols = tuple(protocols)
        return func

    return wrap


class Stats:
    """Pass/fail bookkeeping for one script, with a summary and exit code."""

    def __init__(self, name: str, verbose: bool = False, context: dict | None = None):
        self.name = name
        self.verbose = verbose
        self.context = context or {}
        self.results: list[Result] = []
        self.notes: list[str] = []
        self.ok = 0
        self.failures: list[str] = []
        self.skipped: list[str] = []
        self._lock = threading.Lock()
        self.started = time.time()
        print(f"=== {name} ===")
        for line in self._context_lines():
            print(f"  ---- {line}")

    def _context_lines(self) -> list[str]:
        return [f"{k}: {v}" for k, v in self.context.items()]

    # -- recording ---------------------------------------------------------
    def check(
        self, condition: bool, message: str, rule: str = "", detail: str = ""
    ) -> bool:
        """Record one check.

        `message` names the check and must not vary with the outcome: the
        matrix lines columns up by name, so a name that changes when the check
        fails appears as a *missing* result rather than a failing one. Put the
        observed value in `detail`, which is reported either way.
        """
        with self._lock:
            shown = f"{message} ({detail})" if detail else message
            if condition:
                self.ok += 1
                self.results.append(Result(message, PASS, detail=detail, rule=rule))
                if self.verbose:
                    print(f"  ok   {shown}")
            else:
                cited = f"{shown} [{rule}]" if rule else shown
                self.failures.append(cited)
                self.results.append(Result(message, FAIL, detail=cited, rule=rule))
                print(f"  FAIL {cited}")
            return bool(condition)

    def error(self, message: str, exc: BaseException | None = None, rule: str = "") -> None:
        with self._lock:
            detail = f"{message}: {type(exc).__name__}: {exc}" if exc else message
            # Render the clause the same way check() does. It was being stored
            # and not shown, so a cited failure read as an uncited one in the
            # summary -- and whether a failure cites a clause is the property
            # this suite uses to decide how much to trust it.
            cited = f"{detail} [{rule}]" if rule else detail
            self.failures.append(cited)
            self.results.append(Result(message, FAIL, detail=cited, rule=rule))
            print(f"  FAIL {cited}")
            if exc is not None and self.verbose:
                traceback.print_exc()

    def skip(self, message: str, reason: str = "") -> None:
        """Record a check that did not run, and why.

        The reason is what the report shows in place of a result, so a skip
        reads as an explained absence rather than as a silent pass.
        """
        with self._lock:
            detail = f"{message}: {reason}" if reason else message
            self.skipped.append(detail)
            self.results.append(Result(message, SKIP, detail=detail))
            print(f"  SKIP {detail}")

    def note(self, message: str) -> None:
        with self._lock:
            self.notes.append(message)
            print(f"  ---- {message}")

    # -- reporting ---------------------------------------------------------
    def report(self) -> dict:
        return {
            "script": self.name,
            "context": self.context,
            "elapsed": round(time.time() - self.started, 3),
            "passed": self.ok,
            "failed": len(self.failures),
            "skipped": len(self.skipped),
            "results": [r.as_dict() for r in self.results],
            "notes": self.notes,
        }

    def write_report(self, path: str) -> None:
        with open(path, "w") as handle:
            json.dump(self.report(), handle, indent=2)

    def write_html(self, path: str) -> None:
        from . import report as report_module

        report_module.write_run(self.report(), path)

    def write_outputs(self, args) -> None:
        """Honour --report and --html, whichever were asked for."""
        if getattr(args, "report", None):
            self.write_report(args.report)
        if getattr(args, "html", None):
            self.write_html(args.html)

    def finish(self) -> int:
        elapsed = time.time() - self.started
        skipped = f", {len(self.skipped)} SKIPPED" if self.skipped else ""
        print(
            f"--- {self.name}: {self.ok} checks passed, "
            f"{len(self.failures)} failed{skipped}, {elapsed:.1f}s"
        )
        if self.skipped:
            print("    skipped (not passes):")
            for entry in self.skipped:
                print(f"      - {entry}")
        if self.failures:
            print("    failures:")
            for failure in self.failures[:20]:
                print(f"      - {failure}")
            if len(self.failures) > 20:
                print(f"      ... and {len(self.failures) - 20} more")
        return 1 if self.failures else 0


def _with_watchdog(func, timeout: float, *args, **kwargs):
    """Run `func` on a worker thread, abandoning it after `timeout`.

    Several checks here cover conditions whose failure mode is "never
    returns" -- a read that outlives its timeout, a lock that is never
    granted, a connection that stalls mid-message. Running them inline would
    mean the suite hangs instead of reporting the hang, which is the one
    outcome that makes an overnight run worthless.

    The abandoned thread is left running: it is a daemon, it is blocked in a
    syscall that by definition is not returning, and there is no safe way to
    interrupt it. That leaks a thread per hung check, which is acceptable
    precisely because a hung check is already a failure being reported.
    """
    box: dict = {}

    def target():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"did not return within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def run_checks(
    checks: Iterable[Callable],
    stats: Stats,
    *args,
    watchdog: float | None = 30.0,
    on_timeout: Callable[[], None] | None = None,
    **kwargs,
) -> Stats:
    """Run registered check functions, one Result each.

    A check reports by raising: `Skip` for "cannot run here", `AssertionError`
    for a failed expectation, anything else for a check that broke. Returning
    normally is a pass, and a returned string becomes the detail line.
    """
    for func in checks:
        name = getattr(func, "_check_name", func.__name__)
        rule = getattr(func, "_check_rule", "")
        started = time.time()
        try:
            if watchdog:
                detail = _with_watchdog(func, watchdog, *args, **kwargs) or ""
            else:
                detail = func(*args, **kwargs) or ""
            elapsed = time.time() - started
            with stats._lock:
                stats.ok += 1
                stats.results.append(Result(name, PASS, detail, elapsed, rule))
            print(f"PASS  {name}" + (f"\n      {detail}" if detail else ""))
        except Skip as exc:
            elapsed = time.time() - started
            with stats._lock:
                stats.skipped.append(f"{name}: {exc}")
                stats.results.append(Result(name, SKIP, str(exc), elapsed, rule))
            print(f"SKIP  {name}\n      {exc}")
        except AssertionError as exc:
            elapsed = time.time() - started
            cited = f"{exc} [{rule}]" if rule else str(exc)
            with stats._lock:
                stats.failures.append(f"{name}: {cited}")
                stats.results.append(Result(name, FAIL, cited, elapsed, rule))
            print(f"FAIL  {name}\n      {cited}")
        except TimeoutError as exc:
            elapsed = time.time() - started
            cited = f"{exc} [{rule}]" if rule else str(exc)
            with stats._lock:
                stats.failures.append(f"{name}: {cited}")
                stats.results.append(Result(name, FAIL, cited, elapsed, rule))
            print(f"FAIL  {name}\n      {cited}")
            # The abandoned thread is still running, and it is still talking to
            # the target -- a wedged client typically loops. Left alone it goes
            # on generating traffic, which lands in the observation log that
            # later checks assert against: one hung check reported a 413-byte
            # write arriving in 3473 pieces, all but seven of them someone
            # else's. Replacing the target is the only way to get a clean one,
            # since the thread cannot be killed.
            if on_timeout is not None:
                print(f"      replacing the target: {name} left a thread running")
                try:
                    on_timeout()
                except Exception as restart_exc:  # noqa: BLE001
                    stats.note(f"could not replace the target: {restart_exc}")
        except Exception:
            elapsed = time.time() - started
            trace = traceback.format_exc()
            with stats._lock:
                stats.failures.append(f"{name}: unexpected exception")
                stats.results.append(Result(name, FAIL, trace, elapsed, rule))
            print(f"FAIL  {name} (unexpected exception)")
            print("      " + trace.replace("\n", "\n      ").rstrip())
    return stats


def collect(module, protocol: str | None = None) -> list[Callable]:
    """Every registered check in `module`, in definition order.

    Definition order, not alphabetical: the checks in a file are written to
    build on each other, and a reordering that puts a teardown case before the
    setup it depends on produces failures that are entirely the harness's.
    """
    found = [
        obj
        for obj in vars(module).values()
        if callable(obj) and hasattr(obj, "_check_name")
    ]
    found.sort(key=lambda f: f.__code__.co_firstlineno)
    if protocol is not None:
        found = [f for f in found if protocol in f._check_protocols]
    return found


def main(main_fn) -> None:
    """Run a script main, mapping failure modes onto distinct exit codes.

    Exit 3 for a lost connection specifically: the instrument going away is
    not the same event as a check failing, and a suite that conflates them
    reports a flaky bench as a library regression.
    """
    import pyvisa
    from pyvisa import constants, errors

    from testgear import visa as _visa

    try:
        sys.exit(main_fn())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except (ConnectionError, BrokenPipeError) as exc:
        print(f"\nlost the connection: {type(exc).__name__}: {exc}")
        sys.exit(3)
    except _visa.BadCall as exc:
        # This suite called the library wrongly -- typically an argument a
        # pure-Python backend tolerates and a ctypes one rejects. Exit 5 so it
        # is never mistaken for a finding about the backend.
        print(f"\nthis suite made a bad VISA call: {exc}")
        sys.exit(5)
    except errors.VisaIOError as exc:
        if exc.error_code == constants.StatusCode.error_connection_lost:
            print(f"\nlost the connection: {exc}")
            sys.exit(3)
        traceback.print_exc()
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
