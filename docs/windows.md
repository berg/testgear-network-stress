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

Then, from a checkout:

```powershell
uv venv
uv pip install -e C:\path\to\pyvisa-py     # or: uv pip install pyvisa-py
cargo build --release --manifest-path server\Cargo.toml
uv run python run_all.py --backend keysight --reports reports-keysight
```

`run_all.py` is the cross-platform equivalent of `run_all.sh` -- same scripts,
same order, same exit status -- so no bash is required.

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
