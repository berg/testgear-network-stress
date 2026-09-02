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
# build, so it can be compared against NI and R&S on the same kernel rather
# than across an OS boundary where a disagreement might be about the platform.
#
# The download is a BitRock InstallBuilder self-extractor -- an ELF binary, not
# a shell script, so its options are not readable with `strings`. They came
# from running `--help`:
#
#   --mode           gtk | xwindow | text | unattended   (default gtk)
#   --unattendedmodeui none | minimal | minimalWithDialogs
#   --enable-components / --disable-components
#
# `--mode unattended` is the one that matters; the default is a GTK dialog,
# which in a build would simply hang.
RUN set -eux; \
    if [ "$BACKEND" != "keysight" ]; then echo "not a Keysight image, skipping"; exit 0; fi; \
    run=$(ls /vendor/keysight/*.run 2>/dev/null | head -1 || true); \
    if [ -z "$run" ]; then \
        echo "NO KEYSIGHT .run IN vendor/keysight -- the image builds and the run reports it missing"; \
        ls -la /vendor/keysight 2>/dev/null || true; \
        exit 0; \
    fi; \
    # iproute2 before anything else. Their find-available-port.sh shells out to
    # `ss` or `netstat`, and without one the installer logs "Finding available
    # port failed" and then fails to install the Distributed Infrastructure and
    # discovery services -- after which viOpenDefaultRM spends four minutes
    # reaching for services that were never installed.
    apt-get update; \
    apt-get install -y --no-install-recommends iproute2 net-tools; \
    echo "Keysight installer: $run"; \
    cp "$run" /tmp/iols.run; \
    chmod +x /tmp/iols.run; \
    timeout 120 /tmp/iols.run --version </dev/null || true; \
    # --disable-components, not --enable-components. They are not symmetric:
    # naming only iio,iom,lan under --enable still ran the kernel-driver step,
    # while disabling the hardware components explicitly skips it entirely.
    # That step is 69 of the install's 159 seconds and all of it is wasted --
    # it apt-installs a whole C toolchain so dkms can build modules that then
    # fail to insert ("Operation not permitted"), which is the same wall NI
    # hits and which nothing here needs, since every resource is TCPIP.
    #
    # The list is only what wants a kernel module or a display. It stops there
    # on purpose: also disabling remotes, asrl and vxi builds faster still and
    # leaves no discovery service, so TCPIP0 never resolves and the column is
    # a library that cannot open anything.
    #
    # </dev/null and a timeout on every invocation, because this is a `docker
    # build`: there is no TTY, and anything that decides to read stdin blocks
    # forever with no output. It ran fine under podman and hung for eighteen
    # minutes on a GitHub runner, which is exactly the difference an EOF on
    # stdin makes. A timeout turns "the build is wedged" into a failure with a
    # line number.
    #
    # No fallback to the full component set either: that one pulls in the GPIB
    # and PXI kernel drivers, so if the trimmed install is what hung, retrying
    # with more of it is the wrong instinct.
    timeout 1800 /tmp/iols.run --mode unattended --unattendedmodeui none \
        --disable-components guis,usbtmc,pcigpib,usbgpib,vxigpib,pxi </dev/null \
        || echo "IOLS install failed or timed out; the run will report the backend missing"; \
    # The installer prints nothing at all while it works -- 174s of silence on
    # an idle 8-core box, longer on a runner -- so silence is not evidence of a
    # hang, and its own logs are the only account of what happened. Surface the
    # interesting lines rather than leaving them inside the image for someone
    # to go looking for.
    for log in /tmp/kt_iols_suite_install_linux-x64.log /tmp/kt_iols_visa_install_linux-x64.log; do \
        [ -f "$log" ] || continue; \
        echo "--- $(basename "$log")"; \
        grep -iE "error|fail|prerequisite|not found" "$log" | head -8 || true; \
    done; \
    rm -f /tmp/iols.run; \
    # Their own scripts ship with CRLF line endings and will not parse on
    # Linux: kt_iolsvisa_postinstall.sh dies on line 3 with $'\r': command not
    # found, so the VISA post-install configuration never runs and TCPIP0 is
    # never registered. Strip the carriage returns and run the ones that
    # matter again.
    for f in /opt/keysight/iolibs/.scripts/*.sh; do \
        [ -f "$f" ] && sed -i 's/\r$//' "$f" || true; \
    done; \
    if [ -x /opt/keysight/iolibs/.scripts/kt_iolsvisa_postinstall.sh ]; then \
        echo "re-running the VISA post-install that CRLF broke"; \
        timeout 300 bash /opt/keysight/iolibs/.scripts/kt_iolsvisa_postinstall.sh \
            /opt/keysight/iolibs "Ubuntu" </dev/null \
            || echo "  (still unhappy; the run will say so)"; \
    fi; \
    # -- let their own post-install run -----------------------------------
    #
    # Their suite installer fails to install KDI and the discovery service, and
    # without the discovery service there is no TCPIP0 to open. Both failures
    # are container artefacts, and neither is really about Keysight:
    #
    # DistributedInfrastructureService is Go using kardianos/service, and it
    #   logs to syslog. With no /dev/log socket it dies with "Unix syslog
    #   delivery error" -- which *masks* the real error and sends you chasing
    #   the logger instead of the fault.
    #
    # kardianos/service also picks its init backend by looking for
    #   /run/systemd/system. Absent, it falls through to sysv-init and runs
    #   `service <name> start`, which fails, so the post-install aborts before
    #   writing /var/lib/keysight/Distributed Infrastructure/version.ini -- the
    #   marker the discovery-service installer checks for.
    #
    # Give it a syslog sink and that directory and it takes the systemd path,
    # where the systemctl stub above answers. Their post-install then completes
    # and writes the marker itself, which beats forging it: the file wants a
    # section named for the product as the installer names it, [KDI], and
    # guessing that is how an afternoon goes.
    mkdir -p /run/systemd/system; \
    ( python3 -c "import socket,os,threading,time;\
os.path.exists('/dev/log') and os.unlink('/dev/log');\
s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.bind('/dev/log');\
threading.Thread(target=lambda:[s.recv(8192) for _ in iter(int,1)],daemon=True).start();\
time.sleep(3600)" </dev/null >/dev/null 2>&1 & ) ; \
    sleep 2; \
    ( cd /opt/keysight/iolibs/coreq_installers && \
      timeout 900 ./KDI-linux-x64-installer.run --mode unattended \
          --unattendedmodeui minimal --force true \
          </dev/null >/tmp/kdi-install2.log 2>&1 ) \
      || echo "KDI install still unhappy; see /tmp/kdi-install2.log"; \
    test -s "/var/lib/keysight/Distributed Infrastructure/version.ini" \
        && echo "KDI post-install wrote its marker:" \
        && cat "/var/lib/keysight/Distributed Infrastructure/version.ini" \
        || echo "NO KDI MARKER -- the discovery service will refuse to install"; \
    ( cd "/opt/keysight/Distributed Infrastructure" && ./kdi-controller >/tmp/kdi-build.log 2>&1 & ) ; \
    sleep 8; \
    ( cd /opt/keysight/iolibs/coreq_installers && \
      timeout 900 ./discovery-service-linux-x64-installer.run --mode unattended \
          --unattendedmodeui minimal --force true --interfaces lan \
          </dev/null >/tmp/ds-install.log 2>&1 ) \
      || echo "discovery-service install failed; see /tmp/ds-install.log"; \
    pkill -f kdi-controller || true; \
    pkill -f "bind..dev.log" 2>/dev/null || true; \
    rm -f /dev/log; \
    test -x /opt/keysight/iolibs/ds/io-ds \
        && echo "discovery service installed: io-ds" \
        || echo "NO io-ds -- TCPIP0 will not resolve and the run will say so"; \
    ldconfig || true; \
    rm -rf /var/lib/apt/lists/*; \
    echo "installed Keysight libraries:"; \
    find / -name "libktvisa32.so*" 2>/dev/null | head || true


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
