#!/usr/bin/env python3
"""Put ``side = "bottom"`` floorplan parts on the back of the board.

A carrier that mounts component-side down onto a host board has an 8 mm gap
under every top-side part; a header that must be plugged by hand has to face
the other way. That is a placement fact like any other, so it lives in
``[[floorplan.place]]``::

    [[floorplan.place]]
    ref      = "J2"
    x        = 50.5
    y        = 52.0
    rotation = 0
    side     = "bottom"     # optional; "top" (default) or "bottom"

Why this is its own pass and not part of ``floorplan.py``: the placement loop
(``place.py``) runs ``repair_pads`` after every optimiser call, and that pass
rewrites every pad's footprint-local ``(at ...)`` from the library. A flipped
footprint stores MIRRORED local pad coordinates, so repairing it from the
library un-mirrors the pads while the body stays on B.Cu -- every pad of the
part lands on the wrong side of its own centreline. Flipping after the loop
sidesteps that; the optimiser never sees a bottom-side part, and the anchor
strategy holds the part's position and rotation regardless.

Runs after ``place.py`` and before ``check_placement`` (which reads B.CrtYd
for flipped parts), ``zones`` and ``fanout`` (whose bottom-side handling
exists for exactly this case).

Idempotent: a part already on the requested side is left alone, and the
position/rotation from board.toml are re-asserted either way, so a second run
writes the same board. Pads flip with the footprint (``FOOTPRINT::Flip``),
THT pads stay on ``*.Cu``.

Needs ``pcbnew`` (KiCad 9/10 -- ``FLIP_DIRECTION_TOP_BOTTOM``). --netlist is
accepted for contract uniformity and unused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _lib

NAME = "flip_sides"


def wanted_sides(cfg: dict) -> dict[str, str]:
    """{ref: side} for every [[floorplan.place]] entry, validated."""
    out: dict[str, str] = {}
    for i, e in enumerate((cfg.get("floorplan") or {}).get("place") or []):
        ref = str(e.get("ref", "")).strip()
        side = e.get("side", "top")
        if side not in ("top", "bottom"):
            _lib.fail(f"[[floorplan.place]] #{i + 1} ({ref}): side must be \"top\" or "
                      f"\"bottom\", got {side!r}")
        out[ref] = side
    return out


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found")
    cfg = _lib.load_config(args.config)
    sides = wanted_sides(cfg)
    bottom = sorted(r for r, s in sides.items() if s == "bottom")
    if not bottom:
        print("  no side = \"bottom\" entries -- nothing to flip", file=sys.stderr)
        _lib.emit(NAME, board=str(board), flipped=[], already=[], nets=_lib.assert_net_table(board))
        return 0

    import pcbnew

    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew returned None loading {board}")
    frame = _lib.board_frame(b)
    by_ref = {fp.GetReference(): fp for fp in b.GetFootprints()}
    missing = [r for r in bottom if r not in by_ref]
    if missing:
        _lib.fail(f"side = \"bottom\" refs not on board: {missing}")

    plan = {str(e["ref"]).strip(): e for e in cfg["floorplan"]["place"]}
    flipped, already = [], []
    for ref in bottom:
        fp = by_ref[ref]
        e = plan[ref]
        kx, ky = _lib.to_kicad_xy(frame, float(e["x"]), float(e["y"]))
        pos = pcbnew.VECTOR2I(pcbnew.FromMM(kx), pcbnew.FromMM(ky))
        if fp.IsFlipped():
            already.append(ref)
        else:
            fp.Flip(pos, pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
            flipped.append(ref)
        # Re-assert the floorplan pose after the flip so the result does not
        # depend on what KiCad's Flip did to the orientation sign.
        fp.SetPosition(pos)
        fp.SetOrientationDegrees(float(e.get("rotation", 0.0)))
        fp.SetLocked(bool(e.get("locked", True)))

    if flipped:
        pcbnew.SaveBoard(str(board), b)
    nets = _lib.assert_net_table(board)

    for ref in flipped:
        fp = by_ref[ref]
        print(f"  {ref}: flipped to {b.GetLayerName(fp.GetLayer())}", file=sys.stderr)
    for ref in already:
        print(f"  {ref}: already on the back, pose re-asserted", file=sys.stderr)

    _lib.emit(NAME, board=str(board), flipped=flipped, already=already, nets=nets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
