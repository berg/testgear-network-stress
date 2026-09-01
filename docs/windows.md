# Running on Windows (Keysight, TekVISA)

Keysight IO Libraries and TekVISA are the two implementations this suite
cannot reach from macOS or Linux containers, and both are worth having: every
finding so far rests on two vendor implementations agreeing, and a third
independent one is the cheapest way to harden that.

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

## In CI

`.github/workflows/_leg-windows.yml` does all of the above on a
`windows-latest` runner: builds the mock, installs the driver from the private
store, probes it, and runs both transports. It sets `TESTGEAR_PORTMAP=1`
explicitly rather than letting the probe decide -- if 111 ever stops being
bindable there, that should be a hard failure rather than a silent fall back to
a resource name only pyvisa-py accepts.

The silent-install arguments live in the store's `manifest.json` and are
**unverified**; see [`ci.md`](ci.md). Verifying them on a throwaway VM, once,
is the thing that unblocks those two columns.

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
