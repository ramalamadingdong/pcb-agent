# pcb-agent build container: KiCad (with the SWIG pcbnew module), a JRE,
# Freerouting, and uv. Headless on purpose — no GUI, no X11, no VNC. The
# container builds; the human reviews the board in a native KiCad install.
#
# Verified 2026-08-30: both kicad/kicad:9.0 (pcbnew 9.0.9, Debian 12,
# Python 3.11) and kicad/kicad:10.0 (pcbnew 10.0.5, Debian 13, Python 3.13)
# ship the SWIG bindings in system dist-packages, importable by any user
# including arbitrary non-root UIDs. Both are linux/amd64 only (Apple
# Silicon runs them under emulation).
#
# The default base is 10.0, NOT 9.0: the container must match the KiCad
# major you review with, or it cannot read the boards this repo produces —
# a KiCad 9 pcbnew returns None loading a (version 20260206) board written
# by KiCad 10, which is exactly the host/container drift scripts/doctor.py
# now warns about. On a KiCad 9 host, pin the base back via KICAD_BASE.
#
# Every version is pinned. The base is pinned by digest as well as tag; the
# JRE tarball and the Freerouting jar are checksum-verified at build time.
# When bumping any of these, bump the same values in the Makefile — it passes
# them as build args, so `make setup` is the source of truth and these
# defaults only serve a bare `docker build`.

ARG KICAD_BASE=kicad/kicad:10.0@sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de
FROM ${KICAD_BASE}

USER root

# curl + ca-certificates to fetch the pinned artifacts; make so `./run.sh
# make check` works the same inside and outside.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl make \
    && rm -rf /var/lib/apt/lists/*

# --- JRE: Eclipse Temurin 25, pinned tarball + sha256 (x64 — the base image
# --- is amd64-only, so no per-arch selection is needed). 25, not 21:
# --- Freerouting 2.3.0 is compiled for class-file 69 = Java 25, and dies on
# --- 21 with UnsupportedClassVersionError (measured, not assumed).
ARG TEMURIN_URL="https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4.1%2B1/OpenJDK25U-jre_x64_linux_hotspot_25.0.4.1_1.tar.gz"
ARG TEMURIN_SHA256=1731a34baadec5479258ea0202e4d5d865d2efeee60cb0c7d7eb056fe96ca219
RUN curl -fsSL -o /tmp/jre.tgz "$TEMURIN_URL" \
    && echo "$TEMURIN_SHA256  /tmp/jre.tgz" | sha256sum -c - \
    && mkdir -p /opt/java \
    && tar -xzf /tmp/jre.tgz -C /opt/java --strip-components=1 \
    && rm /tmp/jre.tgz
ENV JAVA_HOME=/opt/java
ENV PATH=/opt/java/bin:$PATH

# --- Freerouting: pinned release jar, checksum-verified. 2.3.0, and the
# --- choice is forced: 1.9.0 wins a routing-quality A/B on a real 4-layer
# --- board (24 vs 28 unconnected, deterministic self-stop) but CANNOT run
# --- headless — its main() calls Toolkit.getScreenSize() even in -de/-do
# --- batch mode and throws HeadlessException with no display (measured in
# --- this container). No X11 goes in this image, so 2.3.0 it is. Native
# --- runs with a display can still prefer 1.9.0 via FREEROUTING_JAR —
# --- remembering 1.9.0's DSN parser hangs on `via_keepout` sections
# --- (export keepouts plain for it), while 2.3.0 parses the full style.
ARG FREEROUTING_VERSION=2.3.0
ARG FREEROUTING_SHA256=3cf18d608437740bc497db6b8ef5888e2e60a08de0def20691d1bad0c0e0ee24
RUN mkdir -p /opt/freerouting \
    && curl -fsSL -o "/opt/freerouting/freerouting-${FREEROUTING_VERSION}.jar" \
        "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" \
    && echo "$FREEROUTING_SHA256  /opt/freerouting/freerouting-${FREEROUTING_VERSION}.jar" | sha256sum -c -
ENV FREEROUTING_JAR=/opt/freerouting/freerouting-${FREEROUTING_VERSION}.jar

# --- uv, pinned. Copied from the official static image, no installer script.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /usr/local/bin/

# --- Non-root. The base image already has kicad (1000:1000); remap it to the
# --- caller's UID/GID so files written to the mounted volume are theirs.
# --- run.sh additionally passes --user at runtime, which works for any UID —
# --- these args cover docker-compose and bare `docker run` too.
ARG PUID=1000
ARG PGID=1000
RUN if [ "$PGID" != "1000" ]; then groupmod -g "$PGID" kicad; fi \
    && if [ "$PUID" != "1000" ]; then usermod -u "$PUID" kicad; fi \
    && chown -R "$PUID:$PGID" /home/kicad

ENV PCB_AGENT_CONTAINER=1
USER kicad
WORKDIR /work
CMD ["bash"]
