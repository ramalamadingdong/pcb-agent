#!/usr/bin/env python3
"""create_pcb — .kicad_sch -> .kicad_pcb with every net assigned.

A thin wrapper around the `kct create-pcb` CLI. Board size, layer count and
the title block come from [build] in board.toml rather than the command line,
so the board a rebuild produces is the board the config describes.

The pass exists as a pass — rather than a line in a shell script — for the
assertion at the end of it. See assert_net_table.

Idempotent: the board and the project files `kct create-pcb` regenerates are
removed before the run, so nothing from a previous (or a failed) run survives
into this one. Note that `kct` is what writes the board, so byte-identical
re-runs are only as reproducible as `kct` itself.

Config consumed: [build] (width_mm, height_mm, layers, title, revision, and
optionally company, spacing_mm, columns, margin_mm), with [board] name as the
title fallback.

The `kct` binary is taken from $KCT, defaulting to "kct".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Running the script by path already puts this directory on sys.path; the
# insert only covers the isolated-mode / -P invocations where it does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import assert_net_table, emit, fail, load_config, pass_parser  # noqa: E402


def log(msg: str) -> None:
    """Progress goes to stderr. Exactly one JSON line goes to stdout."""
    print(msg, file=sys.stderr)


def count_netlist_nets(path: Path) -> int | None:
    """Distinct real nets in netlist.csv, for the JSON line only.

    Reported next to the board's own net-entry count so a reviewer can see
    the two numbers rather than take the tool's word that it wired anything.
    It is deliberately NOT a gate: the board carries net 0 and whatever
    unconnected-pin nets KiCad names for itself, so the two counts differ
    legitimately and by an amount that depends on the design.
    """
    if not path.exists():
        return None
    nets = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Net,"):
            continue
        f = line.split(",")
        if len(f) >= 3 and f[0] and f[0] != "NC":
            nets.add(f[0].strip())
    return len(nets)


def main() -> int:
    ap = pass_parser("create_pcb", board=True, schematic=True, netlist=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        fail(f"{args.config}: config not found or empty")

    build = cfg.get("build") or {}
    for key in ("width_mm", "height_mm"):
        if build.get(key) is None:
            fail(f"board.toml needs [build] {key}")
    layers = int(build.get("layers", 2))
    if layers not in (2, 4):
        fail(f"[build] layers must be 2 or 4, got {layers}")

    sch = Path(args.schematic)
    if not sch.exists():
        fail(f"{sch}: schematic not found — run generate_schematic first")
    board = Path(args.board)
    board.parent.mkdir(parents=True, exist_ok=True)

    title = build.get("title") or (cfg.get("board") or {}).get("name") or sch.stem
    revision = str(build.get("revision", "1.0"))

    # Strip before rebuilding.  create-pcb regenerates the project files too,
    # so a stale .kicad_pro left behind would carry a previous run's netclass
    # edits into a board they no longer describe.
    removed = []
    for path in (board, board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_prl")):
        if path.exists():
            path.unlink()
            removed.append(str(path))

    # kct resolves footprints from ONE footprint directory, named by
    # KICAD_FOOTPRINT_DIR — the directory make_libs vendored the project
    # library into.  Point it there rather than relying on the caller's
    # environment: unset, every footprint reference fails to resolve and the
    # board comes out with nothing on it.  An explicit [libs] out_dir wins so
    # the two passes cannot disagree about where the library is.
    env = dict(os.environ)
    libs = cfg.get("libs") or {}
    if libs.get("out_dir") is not None:
        lib_dir = Path(libs["out_dir"])
        if not lib_dir.is_absolute():
            lib_dir = Path(args.config).resolve().parent / lib_dir
        env["KICAD_FOOTPRINT_DIR"] = str(lib_dir)
        env.setdefault("KICAD_SYMBOL_DIR", str(lib_dir))

    kct = os.environ.get("KCT", "kct")
    cmd = [
        kct,
        "create-pcb",
        str(sch),
        "-o",
        str(board),
        "--width",
        str(float(build["width_mm"])),
        "--height",
        str(float(build["height_mm"])),
        "--layers",
        str(layers),
        "--title",
        str(title),
        "--revision",
        revision,
    ]
    if build.get("company"):
        cmd += ["--company", str(build["company"])]
    if build.get("spacing_mm") is not None:
        cmd += ["--spacing", str(float(build["spacing_mm"]))]
    if build.get("columns") is not None:
        cmd += ["--columns", str(int(build["columns"]))]
    if build.get("margin_mm") is not None:
        cmd += ["--margin", str(float(build["margin_mm"]))]

    log("  " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        fail(f"{kct}: not found. Set $KCT to the kicad-tools CLI.")
    if proc.stdout:
        log(proc.stdout.rstrip())
    if proc.stderr:
        log(proc.stderr.rstrip())
    if proc.returncode != 0:
        fail(f"kct create-pcb failed (exit {proc.returncode})")

    if not board.exists():
        fail(f"kct create-pcb reported success but wrote no {board}")

    # Assert the net table is non-empty after the board write.  One KiCad
    # operation rewrites the board into a stripped format that drops the
    # top-level net table.  KiCad reads it fine, DRC reads it fine, every fab
    # export reads it fine — and the router then reports "nets to route: 0"
    # and does nothing, silently.  Run this after every pass that writes the
    # board, this one included: a board with no nets is not a board.
    net_entries = assert_net_table(board)

    emit(
        "create_pcb",
        board=str(board),
        schematic=str(sch),
        width_mm=float(build["width_mm"]),
        height_mm=float(build["height_mm"]),
        layers=layers,
        title=str(title),
        revision=revision,
        net_entries=net_entries,
        netlist_nets=count_netlist_nets(Path(args.netlist)),
        stripped=removed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
