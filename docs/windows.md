# Running on Windows (TekVISA)

**CI does not do this.** Every leg runs Linux, because all four implementations
CI compares -- pyvisa-py, NI, R&S and Keysight -- have 64-bit Linux builds, and
running them on one kernel means a difference between two columns is a
difference between two implementations rather than between two machines. See
[`ci.md`](ci.md).

What is left here is TekVISA, which is Windows or nothing, and running the
suite on Windows by hand -- which is worth doing occasionally on its own
account, since plenty of people drive pyvisa-py from Windows.

Keysight used to be the other reason for this page. It has a Linux build, so it
moved into the container with the rest; see
[`../vendor/README.md`](../vendor/README.md).

## Setup

Nothing here needs WSL. Windows imposes no privileged-port restriction, so
the mock's portmapper binds port 111 without administrator rights -- which
makes VXI-11 easier to exercise there than on a developer Mac.

1. **Rust**, for the mock server: <https://rustup.rs> (the `x86_64-pc-windows-msvc`
   default is right; it needs the Visual Studio C++ build tools rustup offers
   to install).
2. **uv**: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
3. **Keysight IO Libraries Suite**: <https://www.keysight.com/find/iosuite>.
   Install it *without* the "preferred VISA" option if NI-VISA is also
   present, so `visa32.dll` keeps pointing where you expect; this suite loads
   `ktvisa32.dll` by name and does not care which is preferred.

**That advice is exactly wrong for TekVISA.** `tek` resolves to
`C:\Windows\System32\visa32.dll`, which is the *generic* VISA shim, not a
Tektronix-specific library -- so what it points at is decided by whichever
implementation won the "preferred VISA" argument. On a machine with both,
`--backend tek` may quietly measure Keysight and report it under Tektronix's
name.

So keep the two on separate machines. CI does this by construction: the matrix
gives `keysight` and `tek` their own fresh runners, and that must never be
optimised into one job.

Then, from a checkout:

```powershell
uv venv
uv pip install -e C:\path\to\pyvisa-py     # or: uv pip install pyvisa-py
cargo build --release --manifest-path server\Cargo.toml
uv run python run_all.py --backend keysight --reports reports-keysight
```

`run_all.py` is the sweep itself; `run_all.sh` is now a shim over it. Both take
their script list from `testgear/suite.py`, so no bash is required and the two
cannot drift.

## Set TESTGEAR_PORTMAP explicitly

Windows imposes no privileged-port restriction, so the portmapper binds 111
without administrator rights -- which is what gets you the standard VXI-11
resource name that vendor implementations accept. Pin it rather than letting
the probe decide:

```powershell
$env:TESTGEAR_PORTMAP = "1"
```

Without it a run can quietly fall back to `TCPIP0::host,port::inst0::INSTR`,
which is a pyvisa-py-only extension, and the whole transport then looks
unsupported to any vendor library.

## Folding the results in

The reports directory drops straight into the matrix:

```powershell
uv run python tools\artifact_matrix.py --out matrix.html `
    --reports hislip=reports-hislip vxi11=reports-vxi11
```

Copy `reports-keysight\` back to the machine that generates the page, or run
the generator on Windows -- it only reads JSON.

## Platform caveats

- **Descriptor-leak check**: Windows exposes no `/dev/fd` equivalent, so
  `04_concurrency` reports that check as SKIP rather than silently comparing
  two sentinels and calling the result a pass. The thread-leak half still runs.
- **`TESTGEAR_MOCK_SERVER`**: set this to a prebuilt `.exe` to skip the Rust
  toolchain entirely, if one is built elsewhere for the same target.
