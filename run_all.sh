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
# This is a shim over run_all.py, which does the actual sweep. It used to
# unroll the script list by hand, which made it the third copy of that list and
# the one most likely to drift -- and drift is exactly what happened: two
# scripts once vanished from the vendor columns and the matrix showed the gap
# as "not applicable" rather than "this run crashed".
#
# Kept because the README and a decade of muscle memory name it, and because
# REPORTS/SOAK/ITER as environment variables are a habit worth honouring.
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

if [[ -z "$PY" || ! -x "$PY" ]]; then
    echo "no usable python. Create the venv with:" >&2
    echo "  python3 -m venv .venv && ./.venv/bin/pip install -e /path/to/pyvisa-py" >&2
    exit 4
fi

args=()
[[ -n "${SOAK:-}"    ]] && args+=(--soak "$SOAK")
[[ -n "${ITER:-}"    ]] && args+=(--iterations "$ITER")
[[ -n "${REPORTS:-}" ]] && args+=(--reports "$REPORTS")

# A bare `hislip` or `vxi11` used to pin the sweep to one transport. run_all.py
# spells that --protocol, so translate rather than dropping it silently.
rest=()
for arg in "$@"; do
    case "$arg" in
        hislip|vxi11) args+=(--protocol "$arg") ;;
        *)            rest+=("$arg") ;;
    esac
done

exec "$PY" "$HERE/run_all.py" "${args[@]}" ${rest[@]+"${rest[@]}"}
