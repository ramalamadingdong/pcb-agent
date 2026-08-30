#!/usr/bin/env bash
# Fetch the pinned Freerouting jar into tools/ for people running the
# pipeline natively (the container carries its own copy). Checksum-verified;
# a bad download is deleted, not left lying around to be trusted later.
#
# Version and checksum come from the Makefile (the single source of truth),
# passed via environment: FREEROUTING_VERSION, FREEROUTING_SHA256.
set -euo pipefail

: "${FREEROUTING_VERSION:?set FREEROUTING_VERSION (see Makefile)}"
: "${FREEROUTING_SHA256:?set FREEROUTING_SHA256 (see Makefile)}"

dest_dir="${1:-tools}"
jar="$dest_dir/freerouting-${FREEROUTING_VERSION}.jar"
url="https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar"

mkdir -p "$dest_dir"

if [ -f "$jar" ] && echo "$FREEROUTING_SHA256  $jar" | sha256sum -c - >/dev/null 2>&1; then
    echo "freerouting ${FREEROUTING_VERSION} already in $dest_dir (checksum ok)"
    exit 0
fi

echo "fetching freerouting ${FREEROUTING_VERSION} -> $jar"
curl -fSL -o "$jar" "$url"
if ! echo "$FREEROUTING_SHA256  $jar" | sha256sum -c -; then
    rm -f "$jar"
    echo "CHECKSUM MISMATCH — deleted $jar. Do not fetch this by hand and" >&2
    echo "move on; find out why the published asset changed." >&2
    exit 1
fi
echo "ok: $jar"
