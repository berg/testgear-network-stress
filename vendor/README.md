# Vendor VISA installers

Drop the downloaded NI and R&S packages in here. **Nothing in this directory is
committed** except this file — the licences do not permit redistribution, and
they are large binaries anyway.

Both vendors put their Linux builds behind a click-through, so they cannot be
fetched automatically. Everything else is automated: `../remote-compare.sh`
copies whatever is here to the build host, installs it in a container, and runs
the suite against it.

Architecture matters. Both vendors ship **x86-64 only**, which is why this runs
on a Linux build host rather than in a container on an Apple Silicon Mac.
Anything labelled `arm64`/`aarch64` is the wrong file.

## `ni/` — NI-VISA

The easy route is the repo-configuration package, which is small and pulls the
rest from ni.com at build time (the container has network):

1. <https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html>
2. Pick **Linux**, then the **Ubuntu 22.04** package.
3. You will get a zip containing something like
   `ni-ubuntu2204-drivers-stream.deb`. Put **that .deb** in `ni/`.

The build runs `apt install ni-visa` after installing it. If you would rather
have a fully offline build, put a directory of `.deb` files in `ni/` instead
and they will all be installed with `dpkg -i` before apt is consulted.

Ubuntu 22.04 rather than 24.04 on purpose: NI's 24.04 support is newer and less
well travelled, and the container base is ours to choose.

## `rs/` — R&S VISA

1. <https://www.rohde-schwarz.com/applications/r-s-visa-application-note_56280-148812.html>
2. Accept the terms, download the **Linux amd64 .deb** (named something like
   `rsvisa_7.2.3_amd64.deb`).
3. Put it in `rs/`.

R&S also ship a `.tar.gz`; the `.deb` is the one this expects.

## `keysight/` — Keysight IO Libraries Suite

**Windows.** <https://www.keysight.com/find/iosuite> → the downloads page, and
take the current **Windows x64 IOLS** build. The licence is free, perpetual and
needs no activation. Put the `.exe` in `keysight/`.

Two things the download page says that matter here:

- *"For best interoperability with NI-VISA, it is recommended to install
  NI-VISA first."* Irrelevant for CI, where each backend gets its own fresh
  runner — and that separation is deliberate, see the preferred-VISA trap in
  [`../docs/windows.md`](../docs/windows.md).
- The 2026 release is listed for Windows only. Keysight document a Linux build
  (64-bit only) and this suite knows where to look for it —
  `/opt/keysight/iolibs/libktvisa32.so` — but there is **no Linux download
  linked from that page**, so getting one may mean asking Keysight. Worth the
  ask: see the note below.

## `tek/` — TekVISA

**Windows only**, genuinely — there is no Linux or macOS build.

<https://www.tek.com/en/support/software/driver/tekvisa-connectivity-software-v411>
→ `OpenChoice_TekVisa_Deployment_Package_066093811.exe`, about 100 MB, 64-bit.

Downloading needs a completed Tektronix profile (name, address, organisation)
and acceptance of the licence, and **the approval can take up to one business
day** — so start this one first if you are collecting all four.

## Worth asking Keysight for the Linux build

The Windows legs are the awkward part of this whole arrangement: the
silent-install flags are unverified, a hosted runner cannot reboot, and
`tek` resolves to the generic `visa32.dll` shim rather than a
Tektronix-specific library.

Keysight is the one vendor that could sidestep all of that, because it is the
only one of the two with a Linux build. On Linux it would install into the same
container as NI and R&S, which means no silent-install guesswork, no reboot
question, and — the real prize — a **third vendor column on the same OS as the
other two**. Every finding in `docs/findings.md` rests on vendor agreement, and
agreement between three implementations on one kernel is a stronger claim than
agreement between two.

TekVISA cannot move; it is Windows or nothing.

## What happens next

```bash
./remote-compare.sh            # sync, build, run, and print the matrix
```

A backend whose installer is missing is **reported and skipped**, never quietly
dropped — a comparison table with a column silently absent reads like agreement
between the backends that remain. So it is fine to start with only one of the
two and add the other later.

## Checking what you dropped in

```bash
./remote-compare.sh --check     # says what it found, installs nothing
```
