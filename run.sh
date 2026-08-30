#!/usr/bin/env bash
# Run a command inside the pcb-agent container with this repo mounted at
# /work. Used by the Makefile when the image exists; usable directly:
#
#     ./run.sh python3 scripts/doctor.py
#     ./run.sh make check
#
# Runs as YOUR uid/gid so everything written to the volume is owned by you,
# not root. HOME is pointed at /tmp because an arbitrary --user has no passwd
# entry in the container, and KiCad wants somewhere writable for its config —
# the container is --rm, so that config is deliberately throwaway.
set -euo pipefail

IMAGE="${PCB_AGENT_IMAGE:-pcb-agent}"
here="$(cd "$(dirname "$0")" && pwd)"

tty_flags=()
if [ -t 0 ] && [ -t 1 ]; then tty_flags=(-it); fi

exec docker run --rm "${tty_flags[@]}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -v "$here":/work \
    -w /work \
    "$IMAGE" "$@"
