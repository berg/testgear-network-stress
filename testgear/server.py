# SPDX-License-Identifier: GPL-3.0-or-later
"""Driving the mock server from Python.

Starts `testgear-mock-server`, reads the ports it announces, and wraps its
control socket. One instance per check keeps the checks independent: ports are
ephemeral, so nothing collides, and a check that wedges the server cannot take
its neighbours down with it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRATE = REPO / "server"


class ServerUnavailable(RuntimeError):
    """The mock server binary could not be found or built."""


def _can_bind_privileged_ports() -> bool:
    """Whether port 111 is ours to take.

    Root in a container can have it; a developer machine usually cannot,
    because rpcbind is already there. Probing beats guessing from the uid:
    the answer depends on the port being free as well as on privilege.
    """
    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 111))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def binary_path(build: bool = True) -> Path:
    """Locate the server binary, building it once if needed.

    Prefers an explicit path in TESTGEAR_MOCK_SERVER, then a release build,
    then a debug build. Release first because the soak and throughput checks
    are meaningfully slower against a debug build, and a throughput regression
    that turns out to be the mock's build profile wastes an afternoon.
    """
    override = os.environ.get("TESTGEAR_MOCK_SERVER")
    if override:
        path = Path(override)
        if not path.exists():
            raise ServerUnavailable(f"TESTGEAR_MOCK_SERVER={override} does not exist")
        return path

    for profile in ("release", "debug"):
        candidate = CRATE / "target" / profile / "testgear-mock-server"
        if candidate.exists():
            return candidate

    if not build:
        raise ServerUnavailable("the mock server is not built; run: cargo build --release")
    if shutil.which("cargo") is None:
        raise ServerUnavailable(
            "the mock server is not built and cargo is not installed. "
            "Install Rust (https://rustup.rs) or set TESTGEAR_MOCK_SERVER "
            "to a prebuilt binary."
        )

    print("building the mock server (first run only)...", file=sys.stderr)
    result = subprocess.run(
        ["cargo", "build", "--release"], cwd=CRATE, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ServerUnavailable(f"cargo build failed:\n{result.stderr}")
    return CRATE / "target" / "release" / "testgear-mock-server"


class MockServer:
    """A running mock server, with its control channel."""

    def __init__(
        self,
        pads: tuple[int, ...] = (0, 14, 23),
        host: str = "127.0.0.1",
        proxy: bool = True,
        log_level: str | None = None,
        portmap: bool | None = None,
    ):
        self.host = host
        self._pads = pads
        self._proxy = proxy
        # A portmapper is what makes the VXI-11 server reachable by the
        # standard resource name, and therefore by any VISA other than
        # pyvisa-py, whose "host,port" shorthand is its own extension. It
        # needs port 111, so it is on by default only where that is free:
        # inside a container running as root. Set TESTGEAR_PORTMAP=1/0 to
        # force it either way.
        if portmap is None:
            env = os.environ.get("TESTGEAR_PORTMAP")
            if env is not None:
                portmap = env not in ("", "0", "false", "no")
            else:
                portmap = _can_bind_privileged_ports()
        self._portmap = portmap
        self._log_level = log_level
        self._process: subprocess.Popen | None = None
        self._control: socket.socket | None = None
        self._control_file = None
        self.ports: dict = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "MockServer":
        binary = binary_path()
        env = dict(os.environ)
        if self._log_level:
            env["RUST_LOG"] = self._log_level
        argv = [
            str(binary),
            "--host", self.host,
            "--pads", ",".join(str(p) for p in self._pads),
        ]
        if not self._proxy:
            argv.append("--no-proxy")
        if self._portmap:
            argv.append("--portmap")

        self._process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        # The server prints its ports before it serves, so this read *is* the
        # readiness handshake. Polling a guessed port instead would race the
        # bind and fail about one run in fifty.
        line = self._process.stdout.readline()
        if not line:
            stderr = self._process.stderr.read()
            raise ServerUnavailable(f"the mock server exited at startup:\n{stderr}")
        self.ports = json.loads(line)

        self._control = socket.create_connection(
            (self.host, self.ports["control_port"]), timeout=10
        )
        self._control_file = self._control.makefile("r")
        return self

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._control_file is not None:
                self._control_file.close()
        with contextlib.suppress(Exception):
            if self._control is not None:
                self._control.close()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- resources ---------------------------------------------------------
    @property
    def vxi11_resource(self) -> str:
        return self.ports["vxi11_resource"]

    @property
    def hislip_resource(self) -> str:
        return self.ports["hislip_resource"]

    def resource(self, protocol: str) -> str:
        try:
            return {"vxi11": self.vxi11_resource, "hislip": self.hislip_resource}[protocol]
        except KeyError:
            raise ValueError(f"unknown protocol {protocol!r}") from None

    # -- control channel ---------------------------------------------------
    def _command(self, **request) -> dict:
        if self._control is None:
            raise RuntimeError("the server is not running")
        self._control.sendall(json.dumps(request).encode() + b"\n")
        line = self._control_file.readline()
        if not line:
            raise RuntimeError("the control channel closed unexpectedly")
        reply = json.loads(line)
        if isinstance(reply, dict) and "error" in reply:
            raise RuntimeError(f"mock server rejected {request!r}: {reply['error']}")
        return reply

    def ping(self) -> bool:
        return bool(self._command(cmd="ping").get("ok"))

    def reset(self) -> None:
        """Clear every fault and empty the observation log."""
        self._command(cmd="reset")

    #: Set TESTGEAR_DISABLE_FAULTS=1 to make every arming call a no-op.
    #:
    #: This is a negative control for the suite itself. A check built on fault
    #: injection is only worth what the injection is worth, and a check that
    #: passes whether or not its fault fires is not testing the fault -- it is
    #: testing nothing, quietly. Running the fault-dependent checks with this
    #: set should make them *fail*; any that still pass are suspect.
    @property
    def _faults_disabled(self) -> bool:
        return os.environ.get("TESTGEAR_DISABLE_FAULTS", "") not in ("", "0", "false")

    def set_faults(self, **config) -> dict:
        if self._faults_disabled:
            return self._command(cmd="faults", config={})
        return self._command(cmd="faults", config=config)

    def observed(self) -> list[dict]:
        return self._command(cmd="observed")["events"]

    def clear_observed(self) -> None:
        self._command(cmd="clear_observed")

    def respond(self, query: str, response: str | None, pad: int = 0) -> None:
        """Script an answer for `query`, or remove one with None."""
        self._command(cmd="respond", pad=pad, query=query, response=response)

    def big_reply(self, nbytes: int, pad: int = 0) -> None:
        self._command(cmd="big_reply", pad=pad, bytes=nbytes)

    def set_stb(self, bits: int, pad: int = 0) -> None:
        self._command(cmd="set_stb", pad=pad, bits=bits)

    def hislip_messages(self) -> list[dict]:
        """Every HiSLIP message header seen, in both directions.

        The MessageID rules in IVI-6.1 3.1.2 are invisible through the VISA
        API -- the sequence a client emits is a requirement in its own right,
        and the only place it exists is the wire.
        """
        return self._command(cmd="hislip_messages")["messages"]

    def set_hislip_faults(self, **config) -> None:
        self._command(
            cmd="hislip_faults", config={} if self._faults_disabled else config
        )

    @contextlib.contextmanager
    def hislip_faults(self, **config):
        """Arm message-level HiSLIP faults for a block, then disarm."""
        self.set_hislip_faults(**config)
        try:
            yield self
        finally:
            with contextlib.suppress(Exception):
                self.set_hislip_faults()

    def vxi11_calls(self) -> list[dict]:
        """Every device_write / device_read the client sent, with its flags.

        The operation flags and timeouts (VXI-11 B.5.3, B.5.4) are
        requirements the client has to meet and are invisible from the API:
        whether VI_ATTR_TERMCHAR_EN actually became termchrset on the wire can
        only be seen here.
        """
        return self._command(cmd="vxi11_calls")["calls"]

    def set_vxi11_faults(self, **config) -> None:
        """Arm the RPC-level VXI-11 faults (error codes, maxRecvSize, ...)."""
        self._command(
            cmd="vxi11_faults", config={} if self._faults_disabled else config
        )

    @contextlib.contextmanager
    def vxi11_faults(self, **config):
        """Arm RPC-level faults for a block, then disarm them.

        Cleared by sending an empty config rather than by restoring a
        snapshot: these are one-shot by nature -- "answer the next
        device_read with error 4" -- so there is no prior value that means
        anything once the block is over.
        """
        self.set_vxi11_faults(**config)
        try:
            yield self
        finally:
            with contextlib.suppress(Exception):
                self.set_vxi11_faults()

    @contextlib.contextmanager
    def faults(self, **config):
        """Arm faults for the duration of a block, then put them back.

        Restores the *snapshot* taken on the way in, rather than setting the
        touched keys to None. None means "leave this knob unchanged" in the
        control protocol -- it is how a caller sets one knob without naming
        the rest -- so clearing by nulling silently leaves every fault armed.
        That cost an afternoon: one check turned on one-byte-per-segment
        forwarding, and every check after it timed out and was investigated
        as a transport bug.
        """
        before = self.set_faults()
        self.set_faults(**config)
        try:
            yield self
        finally:
            with contextlib.suppress(Exception):
                self.set_faults(**before)

    # -- observation helpers ----------------------------------------------
    def writes(self, pad: int | None = None) -> list[str]:
        """Just the command text the instrument was written, in order."""
        return [
            e["data"]
            for e in self.observed()
            if e["op"] == "write" and (pad is None or e["pad"] == pad)
        ]

    def count(self, op: str, pad: int | None = None) -> int:
        return sum(
            1
            for e in self.observed()
            if e["op"] == op and (pad is None or e.get("pad") == pad)
        )

    def wait_for(self, predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
        """Poll the observation log until `predicate(events)` holds.

        For the asynchronous paths -- an SRQ the instrument raises, a device
        clear that arrives on another channel -- where asserting immediately
        would be asserting on a race.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.observed()):
                return True
            time.sleep(interval)
        return False


@contextlib.contextmanager
def mock_server(**kwargs):
    """A running mock server for the duration of a block."""
    server = MockServer(**kwargs)
    try:
        yield server.start()
    finally:
        server.stop()
