# SPDX-License-Identifier: GPL-3.0-or-later
"""VISA-level helpers that hold for any backend.

Nothing in here may import a backend's internals at module scope. The suite's
whole value is that the same check runs against pyvisa-py, NI-VISA and R&S
VISA; an import of `pyvisa_py.protocols` at the top of a shared module makes
every check that touches it unrunnable everywhere else, and the failure looks
like the *backend* being broken rather than the harness.

Where a check genuinely needs to see below the VISA layer -- and a few do,
because the mechanism of a finding lives in `protocols/rpc.py` -- it declares
that with `requires_pyvisa_py()` and skips cleanly elsewhere.
"""

from __future__ import annotations

import contextlib
import os
import warnings

import pyvisa
from pyvisa import constants, errors

from .harness import Skip

# VI_SUCCESS_MAX_CNT and friends are statuses this suite provokes on purpose.
warnings.filterwarnings("ignore", category=errors.VisaIOWarning)

SRQ = constants.EventType.service_request
QUEUE = constants.EventMechanism.queue
HANDLER = constants.EventMechanism.handler


class _NotImplemented:
    """Stand-in status for an operation that raised NotImplementedError.

    A VISA operation that is not supported is supposed to answer
    `VI_ERROR_NSUP_OPER`; raising a bare Python exception out of the library
    is an API-contract break in its own right, and one worth reporting rather
    than crashing on. Comparing unequal to every StatusCode means a check
    that expected success fails with this printed, which is exactly the
    right outcome.
    """

    def __repr__(self) -> str:
        return "NotImplementedError (no VISA status)"

    def __eq__(self, other) -> bool:
        return isinstance(other, _NotImplemented)

    def __hash__(self) -> int:
        return hash("_NotImplemented")


NOT_IMPLEMENTED = _NotImplemented()


def call(fn, *args, **kwargs):
    """Call a visalib operation, returning ``(value, status)``.

    Backends raise on a non-success status, which gets in the way when the
    status is exactly what is under test. Turn it back into a return value.
    ``value`` is None for operations that only produce a status.

    A `NotImplementedError` escaping the backend becomes NOT_IMPLEMENTED
    rather than propagating: pyvisa-py raises it from `viFlush` on VXI-11
    sessions, and letting that kill the process costs every remaining check
    in the file to report one gap.
    """
    try:
        result = fn(*args, **kwargs)
    except errors.VisaIOError as exc:
        return None, exc.error_code
    except NotImplementedError:
        return None, NOT_IMPLEMENTED
    if isinstance(result, tuple):
        return result[0], result[-1]
    return None, result


def status(fn, *args, **kwargs):
    """As :func:`call`, keeping only the status code."""
    return call(fn, *args, **kwargs)[1]


def requires_pyvisa_py():
    """Import pyvisa-py's protocol internals, or skip.

    For the handful of checks whose subject is a mechanism below the VISA
    layer. Everything else should be written against the API so it can run
    everywhere.
    """
    try:
        from pyvisa_py import protocols  # noqa: F401

        return protocols
    except ImportError as exc:
        raise Skip(f"needs pyvisa-py's internals, which are not importable ({exc})")


def connection_gone_types() -> tuple[type[BaseException], ...]:
    """Exception types that mean the link is gone and retrying is pointless.

    pyvisa-py raises its own `HiSLIPConnectionLost` alongside the standard
    socket errors; other backends surface the same condition as a VisaIOError
    with `error_connection_lost`, which callers check separately.
    """
    types: list[type[BaseException]] = [ConnectionError, BrokenPipeError]
    with contextlib.suppress(ImportError):
        from pyvisa_py.protocols import hislip

        types.append(hislip.HiSLIPConnectionLost)
    return tuple(types)


def is_connection_lost(exc: BaseException) -> bool:
    if isinstance(exc, connection_gone_types()):
        return True
    return (
        isinstance(exc, errors.VisaIOError)
        and exc.error_code == constants.StatusCode.error_connection_lost
    )


def visa_status(exc: BaseException) -> str:
    """A readable rendering of whatever a backend raised."""
    if isinstance(exc, errors.VisaIOError):
        return f"{exc.abbreviation} ({exc.error_code})"
    return f"{type(exc).__name__}: {exc}"


#: SCPI errors that mean the message flow itself went wrong. These are the
#: ones a transport bug produces, so they are treated as failures; anything
#: else (an unsupported command, say) is only worth noting.
DESYNC_ERRORS = {
    -410,  # Query INTERRUPTED
    -420,  # Query UNTERMINATED
    -430,  # Query DEADLOCKED
    -440,  # Query UNTERMINATED after indefinite response
    -363,  # Input buffer overrun
    -365,  # Time out error
}


def drain_errors(inst, limit: int = 50) -> list[str]:
    """Empty the SCPI error queue, returning whatever was in it."""
    found = []
    for _ in range(limit):
        try:
            err = inst.query("SYST:ERR?").strip()
        except Exception:
            break
        if err.startswith(("0,", "+0,")):
            break
        found.append(err)
    return found


def check_errors(inst, stats, context: str = "") -> list[str]:
    """Drain the error queue, failing on anything that smells like a desync."""
    where = f" {context}" if context else ""
    found = drain_errors(inst)
    for entry in found:
        try:
            code = int(entry.split(",")[0])
        except ValueError:
            code = 0
        if code in DESYNC_ERRORS:
            stats.error(f"instrument reported an I/O desync{where}: {entry}")
        else:
            stats.note(f"instrument error{where}: {entry}")
    return found


def open_fd_count() -> int:
    """Number of open file descriptors, for leak checks."""
    for path in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return -1


@contextlib.contextmanager
def session(resolved, resource: str, timeout: int = 5000, open_timeout: int = 10000, **kwargs):
    """Open one session and close it, leaving the ResourceManager alone.

    Only the session is closed. `ResourceManager.close()` closes every session
    the manager owns, so a worker thread closing "its" manager tears down its
    siblings' -- which presents as `Bad file descriptor` from an unrelated
    thread and reads exactly like a library bug.
    """
    rm = resolved.resource_manager()
    inst = rm.open_resource(resource, open_timeout=open_timeout, **kwargs)
    inst.timeout = timeout
    try:
        yield inst
    finally:
        with contextlib.suppress(Exception):
            inst.close()


def supports(inst, query: str) -> bool:
    """Whether the instrument answers `query` at all.

    Probing with an unimplemented query leaves the instrument addressed to
    talk with nothing to say, so the read times out *and* its error queue
    picks up a complaint. Clean up both, or the next check inherits the mess
    and fails for a reason that has nothing to do with it.
    """
    try:
        reply = inst.query(query)
    except Exception:
        with contextlib.suppress(Exception):
            inst.clear()
        drain_errors(inst)
        return False
    if not reply:
        drain_errors(inst)
        return False
    return True


#: Instrument-specific setup that makes a large reply available, keyed by a
#: fragment of *IDN?. Applied only when --prepare is passed.
#:
#: FETC? rather than READ?: both return the same size, but READ? takes a fresh
#: measurement each time, so two reads of it differ in content while matching
#: in length -- which looks exactly like a transport fault and is not one.
PREPARE_RECIPES = {
    "34401A": {
        "commands": [
            "*CLS",
            "CONF:VOLT:DC 10",
            "VOLT:DC:NPLC 0.02",
            "TRIG:SOUR IMM",
            "SAMP:COUN 200",
            "INIT",
        ],
        "big_query": "FETC?",
        "note": "200 stored readings, about 3.2 kB from FETC?",
        "timeout": 30000,
    },
}


def prepare_instrument(inst, stats):
    """Apply a known setup so the large-reply checks can run.

    Returns the query to use, or None if this instrument has no recipe. Says
    exactly what it is about to change, because it changes the measurement
    configuration and nothing else in this suite does -- which is also why it
    is opt-in behind --prepare rather than automatic.
    """
    try:
        idn = inst.query("*IDN?").strip()
    except Exception:  # noqa: BLE001
        return None

    recipe = next((r for key, r in PREPARE_RECIPES.items() if key in idn), None)
    if recipe is None:
        stats.note(f"--prepare: no recipe for {idn}, leaving it alone")
        return None

    stats.note(
        f"--prepare: reconfiguring the instrument ({recipe['note']}). "
        f"This changes its measurement setup: {'; '.join(recipe['commands'])}"
    )
    for command in recipe["commands"]:
        inst.write(command)
    if inst.timeout < recipe["timeout"]:
        inst.timeout = recipe["timeout"]
        stats.note(f"--prepare: raised the timeout to {recipe['timeout']} ms")
    drain_errors(inst)
    return recipe["big_query"]


def resolve_big_query(args, server, inst, stats) -> str | None:
    """Which large-response query to use here, or None if there is not one.

    Against the mock this is always available. Against real hardware it is
    whatever the caller named, and if the instrument does not implement it the
    large-reply checks skip -- loudly, because those checks stayed skipped
    against a 34401A through an entire suite's development without anyone
    noticing.
    """
    if getattr(args, "prepare", False):
        prepared = prepare_instrument(inst, stats)
        if prepared:
            return prepared

    if args.big_query:
        query = args.big_query
    elif server is not None:
        return "TEST:BIG?"          # the mock always has one
    else:
        query = "*LRN?"

    if supports(inst, query):
        return query

    stats.skip(
        f"the large-response checks: {query} is not usable here. Pass "
        f"--big-query to name one that works, or --prepare if this "
        f"instrument has a recipe"
    )
    return None
