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
    echo "pyvisa-py: $PYVISA_PY_TREE ($(git -C "$PYVISA_PY_TREE" describe --always --dirty --tags 2>/dev/null || echo 'not a checkout') on $(git -C "$PYVISA_PY_TREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"
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
    /usr/local/lib/librsvisa.so
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
