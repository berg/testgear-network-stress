#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Prove the vendor VISA actually loaded before running anything against it.
#
# This matters more than it looks. A VISA library that is installed but cannot
# initialise -- NI-VISA without its kernel modules is the likely case here --
# fails at the first `viOpenDefaultRM`, and pyvisa reports that as "backend not
# available", which is indistinguishable from "you forgot to install it". The
# difference is the whole question, so it gets checked explicitly and reported
# before any check runs.
set -uo pipefail

BACKEND="${BACKEND:-py}"
SUITE=/suite
cd "$SUITE"

banner() { printf '\n=== %s\n' "$*"; }

# NI-VISA's Linux runtime expects its daemons. In a container there is no
# systemd, so start what can be started directly and say what happened rather
# than letting the first check fail for a reason nobody can see.
start_ni_daemons() {
    local started=0
    for daemon in /usr/local/natinst/nipal/bin/nipalsm /usr/sbin/nipalsm; do
        if [[ -x "$daemon" ]]; then
            "$daemon" >/tmp/nipal.log 2>&1 &
            started=1
            echo "started $daemon"
        fi
    done
    if [[ -x /usr/local/vxipnp/linux/NIvisa/USB/NIvisaUSB ]]; then
        echo "(NI USB passport present, not needed for TCPIP)"
    fi
    [[ $started -eq 0 ]] && echo "no NI daemon binary found to start"
    # NI's kernel modules cannot be loaded from an unprivileged container and
    # are not needed for TCPIP resources. Say so, so their absence in the logs
    # is not mistaken for the cause of a later failure.
    if ! lsmod 2>/dev/null | grep -q nipalk; then
        echo "nipalk kernel module not loaded (expected in a container; TCPIP does not need it)"
    fi
    return 0
}

# Keysight's IO libraries need their services running before any interface is
# configured. Without them viOpenDefaultRM succeeds and every viOpen fails with
# VI_ERROR_INTF_NUM_NCONFIG -- "the interface type is valid but the specified
# interface number is not configured" -- which is a confusing way to say that
# TCPIP0 does not exist yet.
#
# Their own start_services.sh cannot do it here, twice over: it has CRLF line
# endings and will not parse on Linux at all, and what it does is defer to
# systemctl, which in this image is the stub the NI install needed. So start
# the binaries directly, the same way start_ni_daemons does.
start_keysight_services() {
    # First, and by far the most important line in this function.
    #
    # libktvisa32 blocks inside viOpenDefaultRM waiting on two named events --
    # "IO libraries ready for IO" and "viFindRsrc ready" -- which a normal
    # installation has the IO Control service set at boot. With no systemd
    # there is nobody to set them, and the wait is not short: it times out
    # after exactly 240 seconds and then carries on regardless. Seventeen
    # scripts times two transports times four minutes is the difference
    # between a leg that runs and a leg that never finishes.
    #
    # SetIOEvents sets them directly. viOpenDefaultRM goes from 240.1s to 0.1s.
    if [[ -x /opt/keysight/iolibs/SetIOEvents ]]; then
        (cd /opt/keysight/iolibs && ./SetIOEvents) || \
            echo "SetIOEvents failed; expect a 240s stall in every viOpenDefaultRM"
    else
        echo "no SetIOEvents binary: viOpenDefaultRM will stall 240s per process"
    fi

    # These services are Go, using kardianos/service, and they log to syslog.
    # With no /dev/log they fail with "Unix syslog delivery error" -- which is
    # worse than it sounds, because it masks whatever the real error was. A
    # sink that reads and discards is enough; /run/systemd/system makes the
    # same library take the systemd path, where the stub above answers.
    mkdir -p /run/systemd/system
    if [[ ! -S /dev/log ]]; then
        python3 -c "import socket,os,threading,time
os.path.exists('/dev/log') and os.unlink('/dev/log')
s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM); s.bind('/dev/log')
threading.Thread(target=lambda:[s.recv(8192) for _ in iter(int,1)],daemon=True).start()
time.sleep(86400)" >/dev/null 2>&1 &
        sleep 1
    fi

    # KDI first: the discovery service registers itself with it.
    if [[ -x "/opt/keysight/Distributed Infrastructure/kdi-controller" ]]; then
        (cd "/opt/keysight/Distributed Infrastructure" && \
            ./kdi-controller >/tmp/kdi-controller.log 2>&1 &)
        echo "started kdi-controller"
        sleep 3
    fi

    # Then the discovery service, which is what actually registers TCPIP0.
    # Without it viOpenDefaultRM succeeds and every viOpen answers
    # VI_ERROR_INTF_NUM_NCONFIG. Their own post-install cannot start it: it
    # calls `io-ds -service start`, and io-ds rejects that flag exactly as
    # DistributedInfrastructureService rejects it.
    if [[ -x /opt/keysight/iolibs/ds/io-ds ]]; then
        (cd /opt/keysight/iolibs/ds && ./io-ds >/tmp/io-ds.log 2>&1 &)
        echo "started io-ds (instrument discovery)"
    else
        echo "no io-ds: TCPIP0 will not resolve"
    fi

    local started=0
    for service in /opt/keysight/iolibs/services/*; do
        [[ -x "$service" ]] || continue
        "$service" >"/tmp/$(basename "$service").log" 2>&1 &
        started=$((started + 1))
    done
    if [[ $started -eq 0 ]]; then
        echo "no Keysight service binaries found to start"
        return 0
    fi
    echo "started $started Keysight service(s)"
    # One of them segfaults on startup in a container and the rest carry on;
    # the interfaces come up regardless, so this is reported rather than fatal.
    sleep "${TESTGEAR_KEYSIGHT_SETTLE:-6}"
    local alive
    alive=$(pgrep -c -f "/opt/keysight/iolibs/services/" || true)
    echo "  ${alive:-0} still running after settling"
}

banner "image: BACKEND=$BACKEND"
echo "python: $(python3 --version 2>&1)"
python3 -c 'import pyvisa; print("pyvisa:", pyvisa.__version__)' 2>&1 | tail -1

# The tree under test is mounted, not baked in: it changes on every commit and
# the layer above it in the image pulls a gigabyte from ni.com. Say plainly
# whether it is there. Falling through to "backend not available" from deep
# inside a check is the failure this whole file exists to prevent.
PYVISA_PY_TREE=/pyvisa-py
if [[ -f "$PYVISA_PY_TREE/pyvisa_py/__init__.py" ]]; then
    export TESTGEAR_PYVISA_PY="$PYVISA_PY_TREE"
    # The SHA, in full, and not `git describe`: describe writes the hash as
    # `g3cc4fe9`, where the leading g is git's own "this is a hash" marker and
    # not part of the hash, and it omits the hash entirely when HEAD is on a
    # tag. This line is what someone reads out of a log to check out the tree
    # the run used, so it has to be pasteable.
    pp_sha=$(git -C "$PYVISA_PY_TREE" rev-parse HEAD 2>/dev/null || echo 'not a checkout')
    pp_branch=$(git -C "$PYVISA_PY_TREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
    [[ -n "$(git -C "$PYVISA_PY_TREE" status --porcelain 2>/dev/null)" ]] && pp_sha="$pp_sha-dirty"
    echo "pyvisa-py: $PYVISA_PY_TREE ($pp_sha on $pp_branch)"
else
    echo "pyvisa-py: NOT MOUNTED at $PYVISA_PY_TREE"
    echo "  Mount the checkout under test:"
    echo "    -v /path/to/pyvisa-py:$PYVISA_PY_TREE:ro"
    echo "  Nothing here installs pyvisa-py, so a run without it would test"
    echo "  whatever happened to be importable -- which is the one result this"
    echo "  suite must never produce."
    [[ "${1:-check}" != "check" ]] && exit 4
fi

banner "VISA libraries present"
found_any=0
for candidate in \
    /usr/lib/x86_64-linux-gnu/libvisa.so \
    /usr/lib/libvisa.so \
    /usr/local/vxipnp/linux/lib64/libvisa.so \
    /usr/lib/x86_64-linux-gnu/librsvisa.so \
    /usr/lib/librsvisa.so \
    /usr/local/lib/librsvisa.so \
    /opt/keysight/iolibs/libktvisa32.so \
    /usr/lib/libktvisa32.so
do
    if [[ -e "$candidate" ]]; then
        echo "  $candidate -> $(readlink -f "$candidate")"
        found_any=1
    fi
done
[[ $found_any -eq 0 ]] && echo "  (none found)"

if [[ "$BACKEND" == "ni" ]]; then
    banner "NI runtime"
    start_ni_daemons
fi

# Keysight answers pyvisa's get_library_paths() with an empty tuple, so the
# probe below cannot report its version the way it does for the others. The
# installer leaves one behind; read it, so the report can name what it ran
# against rather than leaving the column unlabelled.
if [[ "$BACKEND" == "keysight" ]]; then
    banner "Keysight runtime"
    [[ -r /opt/keysight/iolibs/version.txt ]] \
        && echo "IO Libraries $(cat /opt/keysight/iolibs/version.txt)"
    start_keysight_services
fi

banner "does the backend load?"
python3 - <<'PY'
import os
import sys
sys.path.insert(0, "/suite")
from testgear import backends

# The tree is mounted, so nothing has put it on sys.path yet. resolve("py")
# is an import of pyvisa_py, and without this it would report "not importable"
# for a checkout that is sitting right there -- which reads as a missing
# install rather than as a probe that looked in the wrong place.
tree = os.environ.get("TESTGEAR_PYVISA_PY")
if tree:
    try:
        backends.use_pyvisa_py_tree(tree)
    except backends.TreeError as exc:
        print(f"  {exc}")
        sys.exit(4)

target = os.environ.get("BACKEND", "py")
resolved = backends.resolve(target)
print(f"  resolve({target!r}): available={resolved.available} locator={resolved.locator}")
if not resolved.available:
    print(f"  reason: {resolved.reason}")
    sys.exit(10)

try:
    rm = resolved.resource_manager()
    print(f"  ResourceManager opened: {rm}")
    try:
        print(f"  library version: {rm.visalib.get_library_paths()}")
    except Exception:
        pass
except Exception as exc:
    # This is the interesting failure: installed but will not initialise.
    print(f"  FAILED to open a ResourceManager: {type(exc).__name__}: {exc}")
    sys.exit(11)

# Opening a ResourceManager is not enough, and believing it was cost an
# afternoon. Keysight opens one happily with no interface configured and then
# answers every viOpen with VI_ERROR_INTF_NUM_NCONFIG -- so the probe passed,
# all seventeen scripts failed for one cause, and the report this file exists
# to make came from reading a container log instead. Resolving a resource name
# is the cheapest call that touches the interface tables, and it needs no
# server to talk to.
try:
    info = rm.resource_info("TCPIP0::127.0.0.1::inst0::INSTR")
    print(f"  TCPIP0 resolves: {info.interface_type!r}")
except Exception as exc:
    print(f"  TCPIP0 does not resolve: {type(exc).__name__}: {exc}")
    print("  The library loaded but has no usable TCPIP interface, so every")
    print("  viOpen would fail for one cause. Reported here rather than as a")
    print("  column of identical failures.")
    sys.exit(11)
PY
load_rc=$?

if [[ $load_rc -ne 0 ]]; then
    banner "the backend is not usable in this container (exit $load_rc)"
    echo "Exit 10 means the library was not found; exit 11 means it was found"
    echo "but would not initialise -- for NI that usually means the runtime"
    echo "wants kernel modules or daemons a container cannot provide."
    # `check` is a diagnosis run, so report and stop. Anything else would run
    # the suite against a backend already known to be broken and fill the
    # report with failures that all have one cause.
    exit $load_rc
fi

command="${1:-check}"
shift || true

case "$command" in
    check)
        banner "backend is usable"
        ;;
    shell)
        exec /bin/bash
        ;;
    run)
        banner "running the suite"
        exec ./run_all.sh --backend "$BACKEND" "$@"
        ;;
    compare)
        banner "running the comparison scripts"
        exec python3 compare.py --backends "$BACKEND" "$@"
        ;;
    *)
        exec "$command" "$@"
        ;;
esac
