#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Root-cause diagnostics for two findings, below the VISA layer.

1. ``rpc._connect`` reports success for a connection the kernel refused,
   because it takes select() readiness as proof of a connection without
   checking SO_ERROR. The failure then surfaces on the first send.
2. ``rpc._recvrecord`` treats a zero-length recv (peer closed) as "no data
   yet". A closed socket is readable forever, so the loop spins at full CPU
   until the VISA timeout expires.
"""

from __future__ import annotations

import os
import resource
import socket
import time

from pyvisa_py.protocols import rpc

from vxi11_mock import DEVICE_READ, Behavior, MockInstrument


def cpu_time() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def dead_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def check_connect_to_refused_port() -> None:
    port = dead_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    start = time.monotonic()
    connected = rpc._connect(sock, "127.0.0.1", port, 2.0)
    elapsed = time.monotonic() - start
    print(f"  _connect() to a refused port returned {connected} in {elapsed:.3f} s")
    try:
        so_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        print(f"  SO_ERROR on that socket: {so_error} ({os.strerror(so_error)})")
    except OSError as exc:
        print(f"  SO_ERROR unavailable: {exc}")
    if connected:
        try:
            sock.sendall(b"x" * 16)
            time.sleep(0.1)
            sock.sendall(b"x" * 16)
            print("  send on the 'connected' socket: no error raised")
        except OSError as exc:
            print(f"  send on the 'connected' socket raised {type(exc).__name__}: {exc}")
    sock.close()


def check_spin_after_peer_close() -> None:
    behavior = Behavior(responses={b"Q?": b"x\n"}, drop_on_proc=DEVICE_READ)
    with MockInstrument(behavior) as inst:
        client = rpc.RawTCPClient(
            inst.host, 0x0607AF, 1, inst.port, open_timeout=2000
        )
        from pyvisa_py.protocols import vxi11

        core = vxi11.CoreClient.__new__(vxi11.CoreClient)
        core.__dict__.update(client.__dict__)
        core.packer = vxi11.Vxi11Packer()
        core.unpacker = vxi11.Vxi11Unpacker(b"")

        error, link, _abort, _max_size = core.create_link(1, 0, 10000, "inst0")
        assert error == 0, error
        core.device_write(link, 5000, 10000, 8, b"Q?\n")

        cpu_before = cpu_time()
        wall_before = time.monotonic()
        error, reason, data = core.device_read(link, 1024, 5000, 10000, 0, 0)
        wall = time.monotonic() - wall_before
        cpu = cpu_time() - cpu_before

    print(
        f"  device_read after the peer closed: error={error} "
        f"after {wall:.2f} s wall, {cpu:.2f} s CPU ({100 * cpu / wall:.0f}% busy)"
    )
    print(f"  returned data type is {type(data).__name__}: {data!r}")


if __name__ == "__main__":
    print("connect to a port nothing is listening on:")
    check_connect_to_refused_port()
    print("\nread after the instrument closed the connection:")
    check_spin_after_peer_close()
