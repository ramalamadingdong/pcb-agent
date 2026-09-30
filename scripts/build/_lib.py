"""Shared helpers for build passes.

Every pass in this directory satisfies the same contract (CONTRACT.md):
takes --board (or --schematic) and --netlist, reads
generalized parameters from board.toml via --config, is idempotent, exits
non-zero on failure, and prints exactly one JSON line on success so the
agent can verify what changed rather than assume.
"""

from __future__ import annotations

import argparse
import json
import os
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
    # Two dialects exist: the numbered table `(net 1 "GND")` (kct and
    # KiCad <= 8 style) and the name-only references `(net "GND")` that
    # pcbnew 10's SaveBoard writes. Either proves nets survived the write;
    # zero of both is the stripped-table disaster.
    n = len(re.findall(r'^\s*\(net\s+\d+\s+"', text, re.M))
    n_named = len(re.findall(r'\(net\s+"[^"]+"\)', text))
    if n <= 1 and n_named == 0:  # net 0 "" is always present, even stripped
        fail(
            f"{board_path}: net table is empty ({n} numbered, {n_named} named "
            "net entries) — a board write stripped it; the router would "
            "silently no-op"
        )
    return max(n, n_named)


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


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write a text build output so a crash leaves the old file, not half a file.

    Written to a sibling temp file and moved over the target with os.replace,
    which is atomic on one filesystem. newline="" so a Windows run cannot
    re-encode line endings (see repair_pads).

    For text writes only. pcbnew.SaveBoard can't be pointed at a temp name:
    it also writes <name>.kicad_pro / .kicad_prl beside it and stamps the temp
    name into the project's meta.filename (measured on KiCad 10.0.5).
    """
    p = Path(path)
    tmp = p.with_name(p.name + ".partial")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, p)


def unrouted_nets(board_path: str | Path, clusters: bool = False) -> dict:
    """What is still unconnected, measured by pcbnew and not by the router.

    Zones are filled IN MEMORY first (nothing is saved): pads that reach each
    other only through a pour are connected, and an unfilled board would
    report every plane net as broken.

    Two numbers, because they count different things and both are real:
      * ``nets``: per net, pad clusters - 1, i.e. the links still missing
        between pads. Found by a transitive walk over GetConnectedItems.
        GetConnectedPads alone returns only pads touching directly, and
        called 113 links missing on a fully routed board.
      * ``ratsnest_unconnected``: GetUnconnectedCount, which also counts
        copper that belongs to no pad (a fanout via, a zone island).
    On the example's routed board both are 0. On its pre-route snapshot
    they are 89 and 93.

    ``clusters=True`` adds, per broken net, its islands of connected pads as
    lists of (ref, pad, x_mm, y_mm), for drawing a ratsnest.
    """
    import pcbnew

    b = pcbnew.LoadBoard(str(board_path))
    if b is None:
        fail(f"pcbnew could not load {board_path} (KiCad major mismatch?)")
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.BuildConnectivity()
    conn = b.GetConnectivity()

    by_net: dict[str, list] = {}
    for fp in b.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() > 0:
                by_net.setdefault(p.GetNetname(), []).append(p)

    missing: dict[str, int] = {}
    islands: dict[str, list] = {}
    for net, pads in by_net.items():
        by_id = {p.m_Uuid.AsString(): p for p in pads}
        reached: set[str] = set()
        groups = []
        for p in pads:
            if p.m_Uuid.AsString() in reached:
                continue
            seen = {p.m_Uuid.AsString()}
            stack = [p]
            while stack:
                for q in conn.GetConnectedItems(stack.pop()):
                    k = q.m_Uuid.AsString()
                    if k not in seen:
                        seen.add(k)
                        stack.append(q)
            group = seen & by_id.keys()
            reached |= group
            groups.append(group)
        if len(groups) > 1:
            missing[net] = len(groups) - 1
            if clusters:
                islands[net] = [
                    [(by_id[k].GetParentFootprint().GetReference(), by_id[k].GetNumber(),
                      pcbnew.ToMM(by_id[k].GetPosition().x),
                      pcbnew.ToMM(by_id[k].GetPosition().y)) for k in sorted(g)]
                    for g in groups
                ]

    out = {
        "nets": dict(sorted(missing.items(), key=lambda t: (-t[1], t[0]))),
        "pad_links_missing": sum(missing.values()),
        "ratsnest_unconnected": conn.GetUnconnectedCount(False),
    }
    if clusters:
        out["clusters"] = islands
    return out


def off_angle(dx_mm: float, dy_mm: float, tol_deg: float = 0.5) -> bool:
    """True if a segment is not within tol of a multiple of 45 degrees."""
    import math

    if math.hypot(dx_mm, dy_mm) < 0.05:     # too short to have a direction
        return False
    ang = math.degrees(math.atan2(dy_mm, dx_mm)) % 45.0
    return min(ang, 45.0 - ang) > tol_deg


def track_stats(board) -> dict:
    """Via count, copper length, and segments not at a multiple of 45 deg.

    ``board`` is a loaded pcbnew BOARD. Freerouting emits 45-degree geometry;
    an off-angle segment was drawn by a completion pass (finish_routes) and
    is worth a look, not an error: it can be a legitimate short escape.
    """
    import math

    import pcbnew

    vias = 0
    length = 0.0
    off: list[dict] = []
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            vias += 1
            continue
        s, e = t.GetStart(), t.GetEnd()
        dx, dy = pcbnew.ToMM(e.x - s.x), pcbnew.ToMM(e.y - s.y)
        seg = math.hypot(dx, dy)
        length += seg
        if isinstance(t, pcbnew.PCB_ARC) or seg < 0.05:
            continue
        if off_angle(dx, dy):
            off.append({"net": t.GetNetname(), "layer": t.GetLayerName(),
                        "x": round(pcbnew.ToMM(s.x), 3), "y": round(pcbnew.ToMM(s.y), 3),
                        "length_mm": round(seg, 3)})
    return {"vias": vias, "track_length_mm": round(length, 2),
            "off_angle_segments": off}


def count_segments(board_path: str | Path) -> int:
    """Copper segment count, for before/after router verification —
    a correct net list plus zero new copper is a silent no-op."""
    text = Path(board_path).read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"^\s*\(segment\s", text, re.M))
