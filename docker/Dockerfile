# SPDX-License-Identifier: GPL-3.0-or-later
#
# One image per VISA implementation under test. Everything runs inside it --
# the mock server too -- so the checks reach the server over loopback exactly
# as they do on a laptop, and nothing depends on container networking.
#
# Ubuntu 22.04 rather than 24.04: NI's 24.04 support is newer and less well
# travelled, and the base is ours to choose. R&S is happy on either.

# ---------------------------------------------------------------------------
# The mock server. Built on bullseye (glibc 2.31) rather than on the runtime
# base, because a binary linked against a newer glibc than the runtime has
# will not start, and that failure looks like a broken mock rather than a
# build-host mismatch.
# ---------------------------------------------------------------------------
FROM docker.io/library/rust:1-bullseye AS server

WORKDIR /build
COPY server/Cargo.toml server/Cargo.lock ./
# Prime the dependency cache against a stub, so editing our own sources does
# not rebuild the whole tree on every run.
RUN mkdir -p src && \
    echo 'fn main() {}' > src/main.rs && \
    echo '' > src/lib.rs && \
    cargo build --release 2>/dev/null || true
COPY server/src ./src
RUN touch src/main.rs src/lib.rs && cargo build --release


# ---------------------------------------------------------------------------
# The runtime: a VISA implementation, python, and the checks.
# ---------------------------------------------------------------------------
FROM docker.io/library/ubuntu:22.04

# `ni`, `rs`, or `py`. Selects which installer step runs; everything else is
# shared. `py` installs no vendor library at all, which gives pyvisa-py's own
# column an image built exactly like the vendors' -- same base, same Python,
# same suite -- so a difference between the columns is a difference between the
# implementations and not between two machines.
ARG BACKEND=ni
ENV BACKEND=${BACKEND}
ENV DEBIAN_FRONTEND=noninteractive

# git is not optional here. The provenance block shells out to it to name the
# commit under test, and without it every report would be attributed to a tree
# it could not identify. A result that cannot say what produced it is not
# reproducible, and comparing trees is the whole point.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git \
        ca-certificates curl gnupg \
        libusb-1.0-0 libxml2 udev kmod \
    && rm -rf /var/lib/apt/lists/*

# NI's package scripts call systemctl, which does not exist in a container, so
# dpkg leaves ni-visa unconfigured and the library never appears. A stub that
# succeeds and says what it was asked to do gets the install through without
# hiding the fact that a service was never actually started -- which matters,
# because one of those services is LXI discovery and its absence is a real
# difference from a normal NI installation.
RUN printf '#!/bin/sh\necho "[systemctl stub] ignoring: $*" >&2\nexit 0\n' \
        > /usr/bin/systemctl && chmod +x /usr/bin/systemctl

# The installers, whatever the user dropped in. Copied as a directory so the
# build does not fail when one vendor is absent -- a missing backend must be
# reported by the run, not by the build.
COPY vendor/ /vendor/

# -- NI-VISA ----------------------------------------------------------------
# NI ship a zip of repo-configuration packages, one per distro, in two
# flavours: a pinned quarterly release and a rolling "stream". The pinned one
# is preferred -- a comparison run whose reference implementation silently
# changed version between runs is not a comparison.
#
# A loose .deb (or several) in vendor/ni is honoured too, which is the fully
# offline route.
RUN set -eux; \
    if [ "$BACKEND" != "ni" ]; then echo "not an NI image, skipping"; exit 0; fi; \
    mkdir -p /tmp/ni; \
    if ls /vendor/ni/*.zip >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends unzip; \
        for z in /vendor/ni/*.zip; do unzip -o -j "$z" -d /tmp/ni; done; \
    fi; \
    cp /vendor/ni/*.deb /tmp/ni/ 2>/dev/null || true; \
    repo=""; \
    for pattern in "ni-ubuntu2204-drivers-20*.deb" "ni-ubuntu2204-drivers-stream.deb"; do \
        found=$(ls /tmp/ni/$pattern 2>/dev/null | head -1 || true); \
        if [ -n "$found" ]; then repo="$found"; break; fi; \
    done; \
    if [ -z "$repo" ]; then \
        echo "NO USABLE NI PACKAGE IN vendor/ni -- the image builds and the run reports it missing"; \
        ls -la /tmp/ni || true; \
        exit 0; \
    fi; \
    echo "NI repo package: $repo"; \
    apt-get update; \
    apt-get install -y --no-install-recommends "$repo"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ni-visa; \
    rm -rf /var/lib/apt/lists/* /tmp/ni

# -- R&S VISA ---------------------------------------------------------------
# amd64 only, and chosen explicitly rather than by glob. R&S also publish an
# armhf build for Raspberry Pi, and a wildcard that picked it up would fail
# with a confusing dpkg architecture error rather than an obvious one.
RUN set -eux; \
    if [ "$BACKEND" != "rs" ]; then echo "not an R&S image, skipping"; exit 0; fi; \
    deb=$(ls /vendor/rs/*amd64.deb 2>/dev/null | sort -V | tail -1 || true); \
    if [ -z "$deb" ]; then \
        echo "NO amd64 R&S PACKAGE IN vendor/rs -- the image builds and the run reports it missing"; \
        ls -la /vendor/rs || true; \
        exit 0; \
    fi; \
    for other in /vendor/rs/*.deb; do \
        [ "$other" = "$deb" ] || echo "ignoring $other (not amd64)"; \
    done; \
    echo "R&S package: $deb"; \
    apt-get update; \
    apt-get install -y --no-install-recommends "$deb" || dpkg -i "$deb"; \
    apt-get install -y -f --no-install-recommends || true; \
    rm -rf /var/lib/apt/lists/*

# -- Keysight IO Libraries --------------------------------------------------
# The third vendor column, and the reason there is no Windows leg: Keysight is
# the only one of the two Windows-capable implementations with a 64-bit Linux
# build, so it can be compared against NI and R&S on the same kernel instead of
# across an OS boundary.
#
# UNVERIFIED. Keysight do not publish a single canonical Linux artefact, so
# this accepts what they are known to ship -- a tarball with an installer
# script inside, loose .deb packages, or a .run -- and says which one it found.
# When the real download is in hand, delete the branches it did not need.
RUN set -eux; \
    if [ "$BACKEND" != "keysight" ]; then echo "not a Keysight image, skipping"; exit 0; fi; \
    if [ ! -d /vendor/keysight ] || [ -z "$(ls -A /vendor/keysight 2>/dev/null)" ]; then \
        echo "NO KEYSIGHT PACKAGE IN vendor/keysight -- the image builds and the run reports it missing"; \
        exit 0; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential libtirpc-dev; \
    mkdir -p /tmp/ks; \
    for archive in /vendor/keysight/*.tar.gz /vendor/keysight/*.tgz; do \
        [ -e "$archive" ] || continue; \
        echo "unpacking $archive"; tar -xzf "$archive" -C /tmp/ks; \
    done; \
    cp /vendor/keysight/*.deb /tmp/ks/ 2>/dev/null || true; \
    cp /vendor/keysight/*.run /tmp/ks/ 2>/dev/null || true; \
    installer=$(find /tmp/ks -maxdepth 3 -name "*.run" -o -maxdepth 3 -name "install*" -type f | head -1 || true); \
    if ls /tmp/ks/*.deb >/dev/null 2>&1; then \
        echo "installing Keysight .deb packages"; \
        dpkg -i /tmp/ks/*.deb || apt-get install -y -f --no-install-recommends; \
    elif [ -n "$installer" ]; then \
        echo "running Keysight installer: $installer"; \
        chmod +x "$installer"; \
        # Their installer is interactive by default; this is the flag that has
        # to be confirmed against the real download.
        "$installer" --silent || "$installer" -q || "$installer"; \
    else \
        echo "NOTHING INSTALLABLE IN vendor/keysight -- the run will report it missing"; \
        ls -laR /tmp/ks || true; \
        exit 0; \
    fi; \
    ldconfig || true; \
    rm -rf /var/lib/apt/lists/* /tmp/ks


# -- python -----------------------------------------------------------------
# pyvisa from pip, or from a git ref when a run wants to test what is current
# rather than what is released.
ARG PYVISA_REF=
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    if [ -n "$PYVISA_REF" ]; then \
        python3 -m pip install --no-cache-dir \
            "git+https://github.com/pyvisa/pyvisa@${PYVISA_REF}"; \
    else \
        python3 -m pip install --no-cache-dir pyvisa; \
    fi

# pyvisa-py is NOT copied in. It is the tree under test, so baking it into the
# image would invalidate every layer above it on each new commit -- and the
# layer above it is NI's install, which pulls about a gigabyte from ni.com.
# Mount the checkout at /pyvisa-py and pass --pyvisa-py, which puts it on both
# sys.path and PYTHONPATH, so the subprocesses see it too.
#
# git refuses to operate on a repository owned by another user, which is what a
# mounted tree looks like. Without this the provenance block reports no commit,
# and a result that cannot name what produced it is not reproducible.
RUN git config --global --add safe.directory /pyvisa-py

WORKDIR /suite
COPY testgear/ /suite/testgear/
COPY checks/ /suite/checks/
COPY reproducers/ /suite/reproducers/
COPY compare.py run_all.py run_all.sh /suite/
COPY tools/ /suite/tools/
COPY pyproject.toml /suite/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --from=server /build/target/release/testgear-mock-server /usr/local/bin/

# So testgear/server.py finds the binary without a cargo toolchain present.
ENV TESTGEAR_MOCK_SERVER=/usr/local/bin/testgear-mock-server
ENV PYTHONUNBUFFERED=1

RUN chmod +x /usr/local/bin/entrypoint.sh /suite/run_all.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["check"]
