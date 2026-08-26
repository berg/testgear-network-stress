# SPDX-License-Identifier: GPL-3.0-or-later
"""Network stress and conformance checks for VISA implementations.

The checks are written against the VISA API rather than against any one
backend, so the same run can be pointed at pyvisa-py, NI-VISA, R&S VISA or a
library path -- which is what turns a failure into a *disparity*, a much
stronger claim than a failure alone.

With no resource named, the checks start `server/`'s mock: real HiSLIP and
VXI-11 servers, vendored from ugpibd, backed by a virtual instrument and
fronted by a fault-injecting proxy. Nothing here needs a bench.
"""

from .harness import FAIL, PASS, SKIP, Result, Skip, Stats, check, collect, run_checks
from .server import MockServer, mock_server

__all__ = [
    "FAIL", "PASS", "SKIP", "Result", "Skip", "Stats",
    "check", "collect", "run_checks", "MockServer", "mock_server",
]
