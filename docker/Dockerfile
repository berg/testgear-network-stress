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

# `ni` or `rs`. Selects which installer step runs; everything else is shared.
ARG BACKEND=ni
ENV BACKEND=${BACKEND}
ENV DEBIAN_FRONTEND=noninteractive

# git is not optional here. setuptools-scm shells out to it to derive
# pyvisa-py's version at install time, and the provenance block shells out to
# it again at run time to name the commit under test. Without it the install
# fails outright and, if it did not, every report would be attributed to a
# tree it could not identify.
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

# -- python -----------------------------------------------------------------
# pyvisa from pip; pyvisa-py from the tree under test, installed editable so
# the provenance block reports the checkout and its commit rather than a
# version stamped at package time.
COPY pyvisa-py/ /pyvisa-py/
# git refuses to operate on a repository owned by another user, which is what
# a COPY-ed tree looks like. Without this, setuptools-scm sees no version and
# the provenance block reports no commit.
RUN git config --global --add safe.directory /pyvisa-py
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir pyvisa && \
    python3 -m pip install --no-cache-dir -e /pyvisa-py

WORKDIR /suite
COPY testgear/ /suite/testgear/
COPY checks/ /suite/checks/
COPY reproducers/ /suite/reproducers/
COPY compare.py run_all.sh /suite/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --from=server /build/target/release/testgear-mock-server /usr/local/bin/

# So testgear/server.py finds the binary without a cargo toolchain present.
ENV TESTGEAR_MOCK_SERVER=/usr/local/bin/testgear-mock-server
ENV PYTHONUNBUFFERED=1

RUN chmod +x /usr/local/bin/entrypoint.sh /suite/run_all.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["check"]
