#!/usr/bin/env python3
"""
Pull any unlocked footprint whose courtyard escapes the board outline back inside.

The CMA-ES placement optimiser scores its boundary term on pad extents, so a
part can sit fully inside by pads while its courtyard hangs a millimetre over
the edge -- which `kct placement check` correctly calls an error, and which a
fab would call a part half off the board.  This is the tidy-up pass: translate
each offending footprint by the minimum amount that brings its courtyard inside
the outline, keeping the required edge clearance.

Locked (hand-floorplanned) parts are never moved; if one of those is out of
bounds that is a floorplan bug and should be fixed in board.toml's
``[[floorplan.place]]``, so this reports it instead of silently papering over
it.  It is a warning, not a failure: the next `kct placement check` counts it,
and the placement loop scores that count.

Run after `kct optimize-placement`.  `place.py` does that, every round.

Idempotent: the move is a clamp, so a second run finds nothing outside and
does not rewrite the board.

board.toml schema
-----------------
::

    [placement]
    edge_clearance_mm = 0.3    # optional, default 0.3. Match your DRC rule.

--netlist is accepted for contract uniformity and unused.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import _lib
from kicad_tools.schema.pcb import PCB

NAME = "tuck_in"

DEFAULT_EDGE_CLEARANCE_MM = 0.3


def courtyard_extent(fp) -> tuple[float, float, float, float] | None:
    """Footprint courtyard bounds in board coordinates, rotation applied."""
    pts: list[tuple[float, float]] = []
    for g in fp.graphics:
        if getattr(g, "layer", "") not in ("F.CrtYd", "B.CrtYd"):
            continue
        for attr in ("start", "end", "center"):
            p = getattr(g, attr, None)
            if p:
                pts.append((p[0], p[1]))
        for p in getattr(g, "points", None) or []:
            pts.append((p[0], p[1]))
    if not pts:
        pts = [(p.position[0], p.position[1]) for p in fp.pads]
    if not pts:
        return None

    rad = math.radians(fp.rotation or 0.0)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    xs, ys = [], []
    for lx, ly in pts:
        # KiCad rotates footprint children clockwise about the origin.
        rx = lx * cos_r + ly * sin_r
        ry = -lx * sin_r + ly * cos_r
        xs.append(fp.position[0] + rx)
        ys.append(fp.position[1] + ry)
    return min(xs), min(ys), max(xs), max(ys)


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found")
    cfg = _lib.load_config(args.config)
    clearance = float((cfg.get("placement") or {}).get("edge_clearance_mm", DEFAULT_EDGE_CLEARANCE_MM))

    pcb = PCB.load(board)

    xs, ys = [], []
    for g in pcb.graphics:
        if getattr(g, "layer", None) == "Edge.Cuts":
            for attr in ("start", "end"):
                p = getattr(g, attr, None)
                if p:
                    xs.append(p[0])
                    ys.append(p[1])
    if not xs:
        _lib.fail("no Edge.Cuts geometry found")

    # Outline in the same board-relative frame Footprint.position uses.
    ox, oy = min(xs), min(ys)
    bx0 = min(xs) - ox + clearance
    by0 = min(ys) - oy + clearance
    bx1 = max(xs) - ox - clearance
    by1 = max(ys) - oy - clearance

    moved, stuck = 0, []
    for fp in pcb.footprints:
        ext = courtyard_extent(fp)
        if ext is None:
            continue
        x0, y0, x1, y1 = ext
        dx = dy = 0.0
        if x0 < bx0:
            dx = bx0 - x0
        elif x1 > bx1:
            dx = bx1 - x1
        if y0 < by0:
            dy = by0 - y0
        elif y1 > by1:
            dy = by1 - y1
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue

        if fp.locked:
            stuck.append((fp.reference, dx, dy))
            continue
        fp.position = (round(fp.position[0] + dx, 3), round(fp.position[1] + dy, 3))
        moved += 1

    nets = None
    if moved:
        pcb.save(board)
        nets = _lib.assert_net_table(board)
    print(f"Tucked {moved} footprint(s) back inside the outline", file=sys.stderr)
    for ref, dx, dy in stuck:
        print(f"  WARNING locked part {ref} is out of bounds by ({dx:+.2f}, {dy:+.2f}) mm", file=sys.stderr)
        print("          -> fix its coordinates in board.toml [[floorplan.place]]", file=sys.stderr)

    _lib.emit(
        NAME,
        board=str(board),
        moved=moved,
        edge_clearance_mm=clearance,
        locked_out_of_bounds=[{"ref": r, "dx": round(dx, 3), "dy": round(dy, 3)} for r, dx, dy in stuck],
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
