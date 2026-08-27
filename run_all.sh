#!/usr/bin/env bash
# Run the whole suite. Any non-zero exit means a check failed.
#
#   ./run_all.sh                                  # everything, both transports
#   ./run_all.sh --protocol hislip                # one transport
#   ./run_all.sh --pyvisa-py ~/code/pyvisa-py     # a specific checkout
#   ./run_all.sh --backend ni                     # a different VISA
#   ./run_all.sh -r TCPIP0::10.0.0.5::hislip0::INSTR   # real hardware
#   SOAK=300 ITER=2000 ./run_all.sh               # lean on it harder
#
# Everything after the options is passed to each script, so any option the
# scripts accept works here.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo venv when there is one, otherwise the system python -- inside a
# container the suite is installed globally and there is no venv.
if [[ -n "${PY:-}" ]]; then
    :
elif [[ -x "$HERE/.venv/bin/python" ]]; then
    PY="$HERE/.venv/bin/python"
else
    PY="$(command -v python3)"
fi
SOAK="${SOAK:-60}"
ITER="${ITER:-300}"
REPORTS="${REPORTS:-}"

if [[ -z "$PY" || ! -x "$PY" ]]; then
    echo "no usable python. Create the venv with:" >&2
    echo "  python3 -m venv .venv && ./.venv/bin/pip install -e /path/to/pyvisa-py" >&2
    exit 4
fi

# Which transports to sweep. A --protocol in the arguments pins it to one;
# otherwise both are run, because most of what this suite has turned up is a
# difference between them.
PROTOCOLS=(hislip vxi11)
for arg in "$@"; do
    case "$arg" in
        hislip) PROTOCOLS=(hislip) ;;
        vxi11)  PROTOCOLS=(vxi11) ;;
    esac
done

failed=0
skipped=0
ran=0

run() {
    local name="$1"; shift
    local proto="$1"; shift
    ran=$((ran + 1))
    echo
    echo "==============================================================="
    echo "=== $name [$proto]"
    local report=()
    if [[ -n "$REPORTS" ]]; then
        mkdir -p "$REPORTS"
        report=(--report "$REPORTS/${name%.py}-$proto.json")
    fi
    local out
    out=$("$PY" "$HERE/checks/$name" --protocol "$proto" "${report[@]}" "$@" 2>&1)
    local rc=$?
    echo "$out"
    # Exit 3 is the target going away rather than a check failing, and the two
    # are worth telling apart: a flaky bench reported as a library regression
    # wastes the next person's afternoon.
    case "$rc" in
        0) ;;
        3) echo ">>> lost the connection to the target"; failed=$((failed + 1)) ;;
        *) failed=$((failed + 1)) ;;
    esac
    if grep -q "SKIP" <<<"$out"; then
        skipped=$((skipped + $(grep -c "SKIP" <<<"$out")))
    fi
}

for proto in "${PROTOCOLS[@]}"; do
    run 01_smoke.py        "$proto" "$@"
    run 02_io.py           "$proto" -n "$ITER" "$@"
    run 03_srq.py          "$proto" -n 30 "$@"
    run 04_concurrency.py  "$proto" -n "$ITER" "$@"
    run 05_lock.py         "$proto" -n "$ITER" "$@"
    run 06_terminate.py    "$proto" -n 15 "$@"
    run 07_clear.py        "$proto" -n 40 "$@"
    run 09_remote_local.py "$proto" "$@"
    run 10_lock_semantics.py "$proto" "$@"
    run 12_session_lifecycle.py "$proto" "$@"
    run 13_events.py       "$proto" "$@"
    run 15_required_attributes.py "$proto" "$@"
    run conformance.py     "$proto" "$@"
    run 08_soak.py         "$proto" --duration "$SOAK" --srq-thread "$@"
done

# VXI-11 only: the RPC-layer checks have no HiSLIP counterpart.
for proto in "${PROTOCOLS[@]}"; do
    if [[ "$proto" == "vxi11" ]]; then
        run vxi11_conformance.py vxi11 "$@"
        run 14_vxi11_flags.py    vxi11 "$@"
    fi
    if [[ "$proto" == "hislip" ]]; then
        run 11_hislip_messages.py hislip "$@"
    fi
done

echo
echo "==============================================================="
echo "$ran scripts run"
if [ "$failed" -eq 0 ]; then
    echo "all scripts passed"
else
    echo "$failed script(s) reported failures"
fi
if [ "$skipped" -gt 0 ]; then
    echo "$skipped check(s) were SKIPPED and are not passes -- see the SKIP"
    echo "lines above for why each one could not run."
fi
if [[ -n "$REPORTS" ]]; then
    echo "JSON reports written to $REPORTS"
fi
exit "$failed"
