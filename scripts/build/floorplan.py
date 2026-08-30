#!/usr/bin/env python3
"""Apply the hand floorplan from board.toml and lock those parts.

A placement optimiser minimises wirelength and overlap.  It cannot know that a
module's antenna must hang off a board edge over bare laminate, that an RF
trace has to be short, that a sensor must sit away from the battery current
path, or that a USB-C connector is useless unless its mating face is at the
perimeter.  Those parts are therefore placed by hand from the constraints in
your rev plan and marked ``(locked yes)``; everything else -- decoupling caps,
pull-ups, LED resistors -- is left for the optimiser, which is genuinely
better than a human at packing 50 passives.

Coordinates are BOARD-FRAME millimetres: origin at the BOTTOM-LEFT corner of
the Edge.Cuts outline, X right, Y UP -- the one frame every coordinate in
board.toml speaks, and the frame ``scripts/validate_gerbers.py`` checks.  So
the numbers match a floorplan drawing directly.

``Footprint.position`` in this model is board-relative with the page offset
carried separately in ``_board_origin``, but it inherits KiCad's Y-DOWN
sense, so this pass flips Y on the way in (``_lib.to_kicad_xy``).  Writing a
config coordinate straight into ``position`` mirrors the whole floorplan
vertically against what the checker will verify.

Run after `kct create-pcb`, before the placement loop (`place.py`).

board.toml schema
-----------------
::

    [floorplan]
    # Optional. Extra refs place.py holds fixed with
    # `kct placement fix --strategy anchor`, on top of every ref placed
    # below and the mounting holes.  That pass moves ANY part absent from
    # its anchor list, locked or not.
    anchors = ["U7", "J9"]

    [[floorplan.place]]
    ref      = "U1"     # required. must exist on the board
    x        = 27.5     # required. board-relative mm
    y        = 16.5     # required. board-relative mm
    rotation = 0        # optional, degrees, default 0
    locked   = true     # optional, default true
    note     = "antenna hangs over the keepout band"   # optional, ignored

    [frame]
    # Optional.  When present, the measured outline is checked against it,
    # so a floorplan written for one board size cannot be applied silently
    # to another.
    outline_bbox = [0.0, 0.0, 55.0, 90.0]
    tolerance_mm = 0.5

Idempotent: placements are absolute assignments, and every ref NOT in the
table is explicitly unlocked, so a re-run after an edit cannot leave a stale
anchor behind.  Two runs over the same board.toml produce the same file.

Not ported:
  * Nothing in the source computed placements from the netlist -- the hand
    floorplan was a literal table of refdes and coordinates, and that is what
    board.toml now carries.  The only netlist-independent geometry the source
    derived (the outline origin and its size check) is kept below.
  * The source's per-part rationale comments are board-specific; write them
    as ``note`` on each entry.

--netlist is accepted for contract uniformity and unused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _lib
from kicad_tools.schema.pcb import PCB

NAME = "floorplan"


def _num(entry: dict, where: str, ref: str, key: str, default=None) -> float:
    v = entry.get(key, default)
    if v is None:
        _lib.fail(f"{where} ({ref}): missing '{key}'")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _lib.fail(f"{where} ({ref}): '{key}' must be a number, got {v!r}")
    return float(v)


def load_placements(cfg: dict) -> list[dict]:
    """Validated ``[[floorplan.place]]`` entries, in board.toml order."""
    entries = (cfg.get("floorplan") or {}).get("place") or []
    if not entries:
        _lib.fail(
            "board.toml has no [[floorplan.place]] entries — nothing to place. "
            "Hand-place and lock everything position-critical before the "
            "placement loop runs."
        )

    out: list[dict] = []
    seen: dict[str, int] = {}
    for i, e in enumerate(entries):
        where = f"[[floorplan.place]] #{i + 1}"
        if not isinstance(e, dict):
            _lib.fail(f"{where}: not a table")
        ref = e.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            _lib.fail(f"{where}: missing 'ref'")
        ref = ref.strip()
        if ref in seen:
            _lib.fail(f"{where}: duplicate ref {ref!r} (already placed by entry #{seen[ref] + 1})")
        seen[ref] = i

        locked = e.get("locked", True)
        if not isinstance(locked, bool):
            _lib.fail(f"{where} ({ref}): 'locked' must be true or false, got {locked!r}")

        out.append(
            {
                "ref": ref,
                "x": _num(e, where, ref, "x"),
                "y": _num(e, where, ref, "y"),
                "rotation": _num(e, where, ref, "rotation", 0.0),
                "locked": locked,
            }
        )
    return out


def outline_bounds(pcb) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for g in pcb.graphics:
        if getattr(g, "layer", None) == "Edge.Cuts":
            for attr in ("start", "end"):
                p = getattr(g, attr, None)
                if p:
                    xs.append(p[0])
                    ys.append(p[1])
    if not xs:
        _lib.fail("no Edge.Cuts geometry found on the board")
    return min(xs), min(ys), max(xs), max(ys)


def check_frame(cfg: dict, w: float, h: float) -> None:
    frame = cfg.get("frame") or {}
    bbox = frame.get("outline_bbox")
    if not bbox:
        print("  [frame] outline_bbox not set — skipping the outline size check", file=sys.stderr)
        return
    if len(bbox) != 4:
        _lib.fail(f"[frame] outline_bbox must be [x1, y1, x2, y2], got {bbox!r}")
    want_w = abs(float(bbox[2]) - float(bbox[0]))
    want_h = abs(float(bbox[3]) - float(bbox[1]))
    tol = float(frame.get("tolerance_mm", 0.5))
    if abs(w - want_w) > tol or abs(h - want_h) > tol:
        _lib.fail(
            f"outline is {w:.1f}x{h:.1f}, [frame] outline_bbox says {want_w:.1f}x{want_h:.1f} "
            f"(tolerance {tol} mm) — re-run create-pcb with matching --width/--height, "
            "or fix the frame in board.toml"
        )


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found — run create-pcb first")
    cfg = _lib.load_config(args.config)
    plan = load_placements(cfg)

    pcb = PCB.load(board)

    x0, y0, x1, y1 = outline_bounds(pcb)
    w, h = x1 - x0, y1 - y0
    print(f"Board outline {w:.1f} x {h:.1f} mm at ({x0:.1f}, {y0:.1f})", file=sys.stderr)
    check_frame(cfg, w, h)

    by_ref = {fp.reference: fp for fp in pcb.footprints}
    planned = {p["ref"] for p in plan}
    missing = sorted(planned - set(by_ref))
    if missing:
        _lib.fail(f"refs not on board: {missing}")

    for p in plan:
        fp = by_ref[p["ref"]]
        # Footprint.position is board-relative; the page offset lives in
        # fp._board_origin and is applied by the serialiser. Config is
        # board-frame (bottom-left, Y-UP) — see _lib.board_frame — while
        # kicad_tools positions run Y-DOWN, so flip against the outline
        # height on the way in.
        fp.position = (round(p["x"], 3), round(h - p["y"], 3))
        fp.rotation = float(p["rotation"])
        fp.locked = bool(p["locked"])

    # Everything not in the floorplan is explicitly unlocked so a re-run after
    # an edit cannot leave a stale anchor behind.
    for fp in pcb.footprints:
        if fp.reference not in planned:
            fp.locked = False

    pcb.save(board)
    nets = _lib.assert_net_table(board)

    free = len(pcb.footprints) - len(plan)
    locked = sum(1 for p in plan if p["locked"])
    print(f"Anchored {len(plan)} constrained parts ({locked} locked)", file=sys.stderr)
    print(f"Left {free} parts free for the optimiser", file=sys.stderr)

    _lib.emit(
        NAME,
        board=str(board),
        placed=len(plan),
        locked=locked,
        free=free,
        refs=[p["ref"] for p in plan],
        outline_mm=[round(w, 3), round(h, 3)],
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
