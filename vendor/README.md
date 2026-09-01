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

**Linux, 64-bit.** <https://www.keysight.com/find/iosuite>. The licence is
free, perpetual and needs no activation. Put whatever they give you --
tarball, `.deb`s, or a `.run` -- in `keysight/`.

The Linux build is not linked from the main downloads page, which lists Windows
only; it exists and is worth chasing. It is the reason this suite has no
Windows CI leg at all. On Linux, Keysight installs into the same container as
NI and R&S, so all four implementations run on one kernel against one mock --
and every finding in `docs/findings.md` rests on vendor agreement, where three
implementations agreeing on the same OS is a stronger claim than two agreeing
across a platform boundary.

The install step in `docker/Dockerfile` is written against what Keysight are
known to ship and is **unverified**. It accepts a tarball with an installer
script, loose `.deb`s, or a `.run`, and says which it found; once the real
download is in hand, delete the branches it did not need.

## TekVISA is not here

TekVISA is Windows-only, and everything CI runs is Linux, so it has no column.
`backends.py` still knows about it -- `--backend tek` works for anyone running
the suite by hand on Windows, see [`../docs/windows.md`](../docs/windows.md) --
but nothing automated can reach it.

That is a real cost, honestly stated: it is one fewer independent
implementation. It is outweighed by what dropping Windows buys, which is that
the three vendors that remain are compared on the same kernel rather than two
of them on Linux and one across an OS boundary where a disagreement might
be about the platform.

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
