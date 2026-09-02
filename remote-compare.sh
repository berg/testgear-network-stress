#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Run the suite against the vendor VISA implementations on a Linux build host.
#
#   ./remote-compare.sh --check          # what is installed, and does it load?
#   ./remote-compare.sh                  # build, run, and print the matrix
#   ./remote-compare.sh --backends rs    # just one
#   ./remote-compare.sh --protocol hislip
#
# Why a remote host at all: NI and R&S ship x86-64 Linux binaries only, and the
# development machine here is an Apple Silicon Mac. Emulating x86 to run a VISA
# implementation whose timing behaviour is part of what we are measuring would
# be measuring qemu. A native Linux box is both faster and more honest.
set -uo pipefail

HOST="${TESTGEAR_HOST:-slopbox}"
REMOTE_DIR="${TESTGEAR_REMOTE_DIR:-~/testgear-network-stress}"
CONTAINER="${CONTAINER:-podman}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYVISA_PY="${PYVISA_PY:-$HERE/../pyvisa-py}"

PROTOCOL="vxi11"
BACKENDS="ni rs"
CHECK_ONLY=0
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)     CHECK_ONLY=1; shift ;;
        --backends)  BACKENDS="${2//,/ }"; shift 2 ;;
        --protocol)  PROTOCOL="$2"; shift 2 ;;
        --host)      HOST="$2"; shift 2 ;;
        -h|--help)   sed -n '3,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)           EXTRA+=("$1"); shift ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

# -- what is here to send ----------------------------------------------------
say "vendor packages to send"
shopt -s nullglob
ni_files=("$HERE"/vendor/ni/*)
rs_files=("$HERE"/vendor/rs/*)
shopt -u nullglob
if [[ ${#ni_files[@]} -eq 0 ]]; then
    echo "  ni/: (empty) -- NI-VISA will be reported missing, not silently skipped"
else
    for f in "${ni_files[@]}"; do echo "  ni/$(basename "$f")"; done
fi
if [[ ${#rs_files[@]} -eq 0 ]]; then
    echo "  rs/: (empty) -- R&S VISA will be reported missing, not silently skipped"
else
    for f in "${rs_files[@]}"; do echo "  rs/$(basename "$f")"; done
fi

[[ -d "$PYVISA_PY/pyvisa_py" ]] || die "no pyvisa-py checkout at $PYVISA_PY (set PYVISA_PY)"
tree_desc=$(git -C "$PYVISA_PY" describe --always --dirty --tags 2>/dev/null || echo "not a checkout")
tree_branch=$(git -C "$PYVISA_PY" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
echo "  pyvisa-py under test: $tree_desc on $tree_branch"

# -- ship it -----------------------------------------------------------------
say "syncing to $HOST:$REMOTE_DIR"
ssh -o BatchMode=yes "$HOST" "mkdir -p $REMOTE_DIR" || die "cannot reach $HOST"

# --delete keeps the remote from accumulating files deleted here, which would
# otherwise show up as a stale check still running long after it was removed.
# pyvisa-py/ and reports/ live only on the far end -- one is synced separately
# just below, the other is written by the run -- so --delete must be told to
# leave them alone or it tries to remove them on every sync.
rsync -az --delete \
    --exclude '.venv/' --exclude '__pycache__/' --exclude 'server/target/' \
    --exclude '.git/' --exclude '*.pyc' \
    --exclude 'pyvisa-py/' --exclude 'reports/' \
    "$HERE"/ "$HOST:$REMOTE_DIR/" || die "rsync of the suite failed"

# The tree under test travels with it, so the container installs the same
# commit this machine is looking at rather than whatever is on the far end.
# .git travels with it now: the tree is mounted rather than built in, and the
# provenance block shells out to git in it to name the commit under test.
rsync -az --delete \
    --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
    --exclude 'build/' --exclude '*.egg-info/' \
    "$PYVISA_PY"/ "$HOST:$REMOTE_DIR/pyvisa-py/" || die "rsync of pyvisa-py failed"

# -- build and run -----------------------------------------------------------
reports_local="$HERE/reports"
mkdir -p "$reports_local"
built=()

for backend in $BACKENDS; do
    say "building the $backend image on $HOST"
    if ! ssh -o BatchMode=yes "$HOST" \
        "cd $REMOTE_DIR && $CONTAINER build --build-arg BACKEND=$backend \
         -f docker/Dockerfile -t localhost/testgear-$backend . 2>&1 | tail -30"
    then
        echo "!! the $backend image failed to build; continuing with the others"
        continue
    fi

    say "checking whether $backend loads"
    if ssh -o BatchMode=yes "$HOST" \
        "cd $REMOTE_DIR && $CONTAINER run --rm \
         -v $REMOTE_DIR/pyvisa-py:/pyvisa-py:ro,Z \
         localhost/testgear-$backend check"
    then
        built+=("$backend")
    else
        echo "!! $backend is installed but not usable here -- see the output above."
        echo "   It will be reported as unavailable rather than dropped from the matrix."
    fi
done

if [[ $CHECK_ONLY -eq 1 ]]; then
    say "check only, stopping here"
    echo "usable backends: ${built[*]:-none}"
    exit 0
fi

if [[ ${#built[@]} -eq 0 ]]; then
    die "no vendor backend is usable, so there is nothing to compare against"
fi

# -- run the checks ----------------------------------------------------------
for backend in "${built[@]}"; do
    say "running the checks under $backend ($PROTOCOL)"
    ssh -o BatchMode=yes "$HOST" \
        "cd $REMOTE_DIR && mkdir -p reports && $CONTAINER run --rm \
         -v $REMOTE_DIR/reports:/suite/reports:Z \
         -v $REMOTE_DIR/pyvisa-py:/pyvisa-py:ro,Z localhost/testgear-$backend \
         compare --protocol $PROTOCOL --json /suite/reports/$backend.json \
         --exit-codes /suite/reports/$backend.rc.json \
         ${EXTRA[*]:-}" 2>&1 | tail -40
done

# pyvisa-py's own column comes from the same containers, so every column was
# produced by the same suite on the same kernel. Running it here instead would
# compare a Linux VISA against a macOS one and quietly attribute the platform
# difference to the implementation.
say "running the checks under pyvisa-py, in the same container"
ssh -o BatchMode=yes "$HOST" \
    "cd $REMOTE_DIR && $CONTAINER run --rm \
     -v $REMOTE_DIR/reports:/suite/reports:Z \
     -v $REMOTE_DIR/pyvisa-py:/pyvisa-py:ro,Z localhost/testgear-${built[0]} \
     python3 compare.py --backends py --protocol $PROTOCOL \
     --pyvisa-py /pyvisa-py --json /suite/reports/py.json \
     --exit-codes /suite/reports/py.rc.json ${EXTRA[*]:-}" 2>&1 | tail -20

say "collecting reports"
rsync -az "$HOST:$REMOTE_DIR/reports/" "$reports_local/" || die "could not collect reports"
ls -la "$reports_local"

say "the matrix"
"$HERE/.venv/bin/python" "$HERE/tools/merge_reports.py" \
    "$reports_local" --html "$reports_local/matrix.html" --protocol "$PROTOCOL"
