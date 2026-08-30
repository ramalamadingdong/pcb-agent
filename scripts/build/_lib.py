"""Shared helpers for build passes.

Every pass in this directory satisfies the same contract (see the
repository README): takes --board (or --schematic) and --netlist, reads
generalized parameters from board.toml via --config, is idempotent, exits
non-zero on failure, and prints exactly one JSON line on success so the
agent can verify what changed rather than assume.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


def pass_parser(
    name: str,
    *,
    board: bool = True,
    schematic: bool = False,
    netlist: bool = True,
) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=name)
    if board:
        ap.add_argument("--board", required=True, help="path to .kicad_pcb")
    if schematic:
        ap.add_argument("--schematic", required=True, help="path to .kicad_sch")
    if netlist:
        ap.add_argument("--netlist", default="netlist.csv")
    ap.add_argument("--config", default="board.toml", help="board.toml with the generalized parameters")
    return ap


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def emit(name: str, **changed) -> None:
    """The one JSON line. Everything else a pass prints goes to stderr."""
    print(json.dumps({"pass": name, **changed}))


def fail(msg: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def assert_net_table(board_path: str | Path) -> int:
    """Fail loudly if a board write stripped the top-level net table.

    One KiCad operation rewrites the board into a stripped format that
    drops it. KiCad opens the file fine, DRC runs fine, every fab export
    works — and the router then reports "nets to route: 0" and does
    nothing, silently. Run this after every pass that writes the board.
    """
    text = Path(board_path).read_text(encoding="utf-8", errors="replace")
    n = len(re.findall(r'^\s*\(net\s+\d+\s+"', text, re.M))
    if n <= 1:  # net 0 "" is always present, even in a stripped file
        fail(
            f"{board_path}: net table is empty ({n} net entries) — "
            "a board write stripped it; the router would silently no-op"
        )
    return n


def board_frame(board) -> tuple[float, float]:
    """(x_left, y_bottom) of the Edge.Cuts bbox, in KiCad mm.

    board.toml speaks ONE coordinate frame everywhere: origin at the
    board's bottom-left corner, X right, Y UP — the frame of a drill
    file or gerber exported with the aux origin at that corner, and the
    frame `[frame].outline_bbox` asserts. KiCad pages run Y DOWN, so a
    pass consuming config coordinates converts:
        kicad_x = x_left + bx ; kicad_y = y_bottom - by
    Never write a config rectangle straight into pcbnew coordinates —
    the checker would then inspect the mirror image of where you drew.
    """
    import pcbnew

    bbox = board.GetBoardEdgesBoundingBox()
    return (
        pcbnew.ToMM(bbox.GetLeft()),
        pcbnew.ToMM(bbox.GetBottom()),
    )


def to_kicad_xy(frame: tuple[float, float], bx: float, by: float) -> tuple[float, float]:
    """Board-frame (bottom-left, Y-up) -> KiCad page mm (Y-down)."""
    x_left, y_bottom = frame
    return (x_left + bx, y_bottom - by)


def count_segments(board_path: str | Path) -> int:
    """Copper segment count, for before/after router verification —
    a correct net list plus zero new copper is a silent no-op."""
    text = Path(board_path).read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"^\s*\(segment\s", text, re.M))
