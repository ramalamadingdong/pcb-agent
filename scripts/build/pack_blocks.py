#!/usr/bin/env python3
"""Tetris-pack every free part into a legal spot near where the optimiser put it.

The placement loop (place.py) is a global placer: it minimises wirelength
and keeps the board with the fewest conflicts, but "fewest" is rarely zero,
and direct_connect.py then snaps tagged parts pad-on-pad onto their targets
wherever that lands -- on top of whatever the optimiser had put there. This
pass is the legaliser that follows, and it treats the board as blocks:

  * a FIXED part never moves: everything in ``[[floorplan.place]]``, the
    mounting holes (sized to the screw head, as check_placement measures
    them), ``[floorplan] anchors``, the fiducials, and every Direct-tagged
    part held on an anchored target
  * every other part is a free block of one -- except that a free part and
    the Direct-tagged parts touching it are ONE rigid block, so the pads
    direct_connect joined stay joined

Blocks are placed largest first. Each goes to the legal position nearest
the one the optimiser gave it (a ring search on a ``step_mm`` grid around
it), in its current orientation; only if that costs more than
``rotate_after_mm`` of displacement are the other three rotations of the
whole block tried, and the least displacement wins. Legal means: every
courtyard of the block inside the board outline, and clear -- by
``gap_mm`` -- of every fixed part, every block already placed, every
``[[keepouts]]`` rectangle on that copper side (unless it sets
``allow_pads``), the fiducials' mask rings, and the plug corridor in front
of every fixed ``[[connectors]]`` entry. Sides are respected: a top part
and a bottom part may share an XY spot; a through-hole part occupies both.

This is Tetris legalisation (Hill, 2002) with rigid multi-part blocks:
nearest-legal-spot keeps the optimiser's wirelength decisions, which a
bottom-left pack from a corner would throw away.

After the write the board is reloaded and checked independently: no
courtyard of a free part overlaps anything outside its own block, all of
them are inside the outline and clear of the obstacles, and every Direct
pair still shares copper. The pass's own search is not the evidence.

Idempotent: a block that is already legal where it sits has displacement
zero and stays, so a second run moves nothing and writes nothing.

Runs after ``direct_connect.py --stage post`` and before ``check_placement``.

board.toml schema
-----------------
::

    [pack]
    step_mm         = 0.25   # search grid around each block's wanted spot
    gap_mm          = 0.0    # extra courtyard-to-courtyard clearance
    rotate_after_mm = 3.0    # try other rotations only past this displacement
    rotate          = true   # false: never rotate a block

Needs ``pcbnew`` (KiCad 9/10). The search at the top of this file does not.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import _lib

NAME = "pack_blocks"

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1, KiCad page mm (Y down)

DEFAULTS = {"step_mm": 0.25, "gap_mm": 0.0, "rotate_after_mm": 3.0, "rotate": True}
BUCKET_MM = 5.0


# --------------------------------------------------------------------------
# search (plain rectangles)
# --------------------------------------------------------------------------


def hits(a: Rect, b: Rect) -> bool:
    """Strict overlap: courtyards that only touch are legal, as in KiCad DRC."""
    return a[0] < b[2] - 1e-6 and b[0] < a[2] - 1e-6 and a[1] < b[3] - 1e-6 and b[1] < a[3] - 1e-6


def inside(r: Rect, outer: Rect) -> bool:
    return r[0] >= outer[0] - 1e-6 and r[1] >= outer[1] - 1e-6 and r[2] <= outer[2] + 1e-6 and r[3] <= outer[3] + 1e-6


def grow(r: Rect, m: float) -> Rect:
    return (r[0] - m, r[1] - m, r[2] + m, r[3] + m)


@dataclass
class Variant:
    """One orientation of a block: its courtyards relative to its pivot."""
    rotation: float  # degrees, relative to the block as it is now
    rects: list[Rect]


@dataclass
class Block:
    key: str
    refs: list[str]
    sides: frozenset  # {"F"}, {"B"} or both (through-hole)
    want: tuple[float, float]  # pivot position the optimiser gave it
    variants: list[Variant]  # variants[0] is the current orientation

    def area(self) -> float:
        v = self.variants[0].rects
        x0 = min(r[0] for r in v); y0 = min(r[1] for r in v)
        x1 = max(r[2] for r in v); y1 = max(r[3] for r in v)
        return (x1 - x0) * (y1 - y0)


@dataclass
class Space:
    """Occupied rectangles per copper side, bucketed so a legality test only
    looks at what is nearby."""
    outline: Rect
    gap: float
    cells: dict = field(default_factory=dict)

    def _keys(self, r: Rect):
        for i in range(math.floor(r[0] / BUCKET_MM), math.floor(r[2] / BUCKET_MM) + 1):
            for j in range(math.floor(r[1] / BUCKET_MM), math.floor(r[3] / BUCKET_MM) + 1):
                yield i, j

    def add(self, sides: frozenset, r: Rect, owner: str) -> None:
        for k in self._keys(r):
            self.cells.setdefault(k, []).append((sides, r, owner))

    def blockers(self, sides: frozenset, r: Rect) -> list[str]:
        g = grow(r, self.gap)
        out = []
        for k in self._keys(g):
            for s, o, owner in self.cells.get(k, ()):
                if s & sides and hits(g, o) and owner not in out:
                    out.append(owner)
        return out

    def legal(self, sides: frozenset, rects: list[Rect]) -> bool:
        return all(inside(r, self.outline) for r in rects) and \
            not any(self.blockers(sides, r) for r in rects)


def shifted(rects: list[Rect], x: float, y: float) -> list[Rect]:
    return [(r[0] + x, r[1] + y, r[2] + x, r[3] + y) for r in rects]


def nearest(space: Space, block: Block, v: Variant, step: float, cap: float):
    """(distance, x, y) of the legal pivot nearest block.want, or None.

    Rings of the step grid, outward; a ring's nearest point is k*step away,
    so the search stops once that exceeds the best found (or `cap`).
    """
    wx, wy = block.want
    o = space.outline
    reach = math.hypot(o[2] - o[0], o[3] - o[1])
    best = None
    k = 0
    while k * step <= min(cap, reach) + 1e-9 and (best is None or k * step < best[0] - 1e-9):
        ring = [(0, 0)] if k == 0 else [
            (i, j) for i in range(-k, k + 1) for j in (-k, k)] + [
            (i, j) for i in (-k, k) for j in range(-k + 1, k)]
        for i, j in ring:
            x, y = wx + i * step, wy + j * step
            d = math.hypot(i * step, j * step)
            if best is not None and d >= best[0] - 1e-9:
                continue
            if space.legal(block.sides, shifted(v.rects, x, y)):
                best = (d, x, y)
        k += 1
    return best


def pack(blocks: list[Block], space: Space, step: float, rotate_after: float, rotate: bool):
    """{key: (variant index, x, y, distance)}; raises LookupError naming a
    block that fits nowhere on the board."""
    out = {}
    order = sorted(blocks, key=lambda b: (-b.area(), b.want[0], b.want[1], b.key))
    for b in order:
        res = nearest(space, b, b.variants[0], step, math.inf)
        choice = (0, *res[1:], res[0]) if res else None
        if rotate and (res is None or res[0] > rotate_after):
            for vi, v in enumerate(b.variants[1:], 1):
                cap = choice[3] if choice else math.inf
                r = nearest(space, b, v, step, cap)
                if r and (choice is None or r[0] < choice[3] - 1e-9):
                    choice = (vi, r[1], r[2], r[0])
        if choice is None:
            raise LookupError(b.key)
        vi, x, y, _ = choice
        for r in shifted(b.variants[vi].rects, x, y):
            space.add(b.sides, r, b.key)
        out[b.key] = choice
    return out


# --------------------------------------------------------------------------
# pcbnew
# --------------------------------------------------------------------------


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()
    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found")
    cfg = _lib.load_config(args.config)
    pc = {**DEFAULTS, **(cfg.get("pack") or {})}
    step, gap = float(pc["step_mm"]), float(pc["gap_mm"])
    if step <= 0:
        _lib.fail("[pack] step_mm must be > 0")

    import pcbnew

    import direct_connect as dc
    from add_mounting_holes import hole_refs
    from check_placement import (DEFAULT_HEAD_DIAMETER_MM, corridor, courtyard_rect,
                                 keepout_rects, nearest_face, rect_of)

    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew returned None loading {board}")
    mm, FM = pcbnew.ToMM, pcbnew.FromMM

    def mmrect(r) -> Rect:
        return tuple(mm(v) for v in r)

    def court(fp) -> Rect:
        if hasattr(fp, "BuildCourtyardCaches"):
            fp.BuildCourtyardCaches()
        return mmrect(courtyard_rect(fp)[0])

    def sides_of(fp) -> frozenset:
        if any(p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH) for p in fp.Pads()):
            return frozenset("FB")
        return frozenset("B" if fp.IsFlipped() else "F")

    by_ref = {fp.GetReference(): fp for fp in b.GetFootprints()}
    tags = dc.load_tags(Path(args.netlist), cfg)
    anchors = dc.anchor_set(cfg)
    held = set(dc.held_refs(Path(args.netlist), cfg, anchors))
    holes = set(hole_refs(cfg))
    fixed = (anchors | held | set(_lib.mechanical_parts(cfg))) & set(by_ref)
    free = sorted(set(by_ref) - fixed)

    # ---- which free parts ride together: a tagged part and what it touches
    def pad_rect(fp, num):
        return [mmrect(rect_of(p.GetBoundingBox())) for p in fp.Pads() if p.GetNumber() == num]

    group = {r: r for r in free}
    touching: list[tuple[str, str, str, str, str]] = []  # sat, pin, target, tpin, net
    for t in tags:
        if t.ref not in by_ref:
            _lib.fail(f"Direct-tagged {t.ref} is not on the board")
        mine = pad_rect(by_ref[t.ref], dc.pad_number(cfg, t.ref, t.pin))
        hit = None
        for tref, tpin in t.candidates:
            if tref in by_ref and any(hits(a, z) for a in mine
                                      for z in pad_rect(by_ref[tref], dc.pad_number(cfg, tref, tpin))):
                hit = (tref, tpin)
                break
        if hit is None:
            _lib.fail(f"{t.ref}.{t.pin} touches none of its targets -- run "
                      "direct_connect.py --stage post before this pass")
        touching.append((t.ref, t.pin, hit[0], hit[1], t.net))
        if t.ref in group and hit[0] in group:
            group[t.ref] = hit[0]  # no chains, so one hop reaches the block's root
        elif t.ref in group:
            _lib.fail(f"{t.ref} is free but touches fixed {hit[0]}; direct_connect should have held it")

    members: dict[str, list[str]] = {}
    for r in free:
        members.setdefault(group[r], []).append(r)

    # ---- obstacles
    outline = mmrect(rect_of(b.GetBoardEdgesBoundingBox()))
    space = Space(outline, gap)
    head = float((cfg.get("mounting_holes") or {}).get("head_diameter_mm", DEFAULT_HEAD_DIAMETER_MM))
    for r in sorted(fixed):
        c = court(by_ref[r])
        if r in holes:  # the screw head, not the hole
            cx, cy, h = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2, head / 2
            c = (cx - h, cy - h, cx + h, cy + h)
        space.add(sides_of(by_ref[r]) if r not in holes else frozenset("FB"), c, r)
    frame = _lib.board_frame(b)
    fd = cfg.get("fiducials") or {}
    ring = float(fd.get("mask_dia_mm", 2.0)) / 2 + float(fd.get("clearance_mm", 0.6))
    for i, (fx, fy) in enumerate(fd.get("positions") or []):
        if f"{fd.get('ref_prefix', 'FID')}{i + 1}" in by_ref:
            continue  # already a fixed footprint
        kx, ky = _lib.to_kicad_xy(frame, float(fx), float(fy))
        space.add(frozenset("FB"), (kx - ring, ky - ring, kx + ring, ky + ring), f"fiducial {i + 1}")
    for ko, r in keepout_rects(b, cfg):
        if ko.get("allow_pads"):
            continue
        names = [str(n).replace("_", ".") for n in (ko.get("layers") or ["F.Cu", "B.Cu"])]
        s = frozenset(x for x, n in (("F", "F.Cu"), ("B", "B.Cu")) if n in names)
        if s:
            space.add(s, mmrect(r), f"keepout {ko.get('name', '?')}")
    board_box = rect_of(b.GetBoardEdgesBoundingBox())
    unguarded = []
    for c in cfg.get("connectors") or []:
        ref = str(c.get("ref") or "")
        if ref not in by_ref:
            continue
        if ref not in fixed:
            unguarded.append(ref)  # its corridor moves with it; check_placement judges it
            continue
        part = courtyard_rect(by_ref[ref])[0]
        face = str(c.get("face", "auto")).lower()
        face = nearest_face(part, board_box) if face == "auto" else face
        corr = corridor(part, board_box, face,
                        FM(float(c.get("mating_depth_mm", 8.0))),
                        FM(float(c.get("lateral_margin_mm", 0.5))),
                        FM(float(c.get("keep_clear_mm", 0.0))))
        # accept_blockers can only name fixed parts, which never move, so
        # it needs nothing here: no free part may enter the corridor.
        space.add(frozenset("FB"), mmrect(corr), f"plug corridor {ref}")

    # ---- blocks, measured by pcbnew at each rotation about their pivot
    blocks: list[Block] = []
    poses: dict[str, tuple] = {}
    for root, refs in sorted(members.items()):
        fps = [by_ref[r] for r in refs]
        for fp in fps:
            poses[fp.GetReference()] = (fp.GetPosition(), fp.GetOrientationDegrees())
        cs = [court(fp) for fp in fps]
        px = (min(c[0] for c in cs) + max(c[2] for c in cs)) / 2
        py = (min(c[1] for c in cs) + max(c[3] for c in cs)) / 2
        pivot = pcbnew.VECTOR2I(FM(px), FM(py))
        variants = []
        for rot in ((0.0, 90.0, 180.0, 270.0) if pc["rotate"] else (0.0,)):
            for fp in fps:
                pos, ang = poses[fp.GetReference()]
                fp.SetPosition(pos)
                fp.SetOrientationDegrees(ang)
                if rot:
                    fp.Rotate(pivot, pcbnew.EDA_ANGLE(rot, pcbnew.DEGREES_T))
            variants.append(Variant(rot, [(c[0] - px, c[1] - py, c[2] - px, c[3] - py)
                                          for c in (court(fp) for fp in fps)]))
        for fp in fps:
            pos, ang = poses[fp.GetReference()]
            fp.SetPosition(pos)
            fp.SetOrientationDegrees(ang)
        sides = frozenset().union(*(sides_of(fp) for fp in fps))
        blocks.append(Block(root, refs, sides, (px, py), variants))

    try:
        plan = pack(blocks, space, step, float(pc["rotate_after_mm"]), bool(pc["rotate"]))
    except LookupError as e:
        blk = next(x for x in blocks if x.key == e.args[0])
        _lib.fail(f"no legal spot anywhere on the board for {blk.refs} -- the board is too full "
                  "for what is fixed on it, or the part is larger than any free region")

    moved, rotated = [], []
    for blk in blocks:
        vi, x, y, dist = plan[blk.key]
        v = blk.variants[vi]
        if dist < 1e-9 and not v.rotation:
            continue
        pivot = pcbnew.VECTOR2I(FM(blk.want[0]), FM(blk.want[1]))
        delta = pcbnew.VECTOR2I(FM(x) - pivot.x, FM(y) - pivot.y)
        for r in blk.refs:
            fp = by_ref[r]
            if v.rotation:
                fp.Rotate(pivot, pcbnew.EDA_ANGLE(v.rotation, pcbnew.DEGREES_T))
            fp.Move(delta)
        moved.append({"block": blk.refs, "moved_mm": round(dist, 3), "rotated": v.rotation})
        if v.rotation:
            rotated.append(blk.key)
        print(f"  {'+'.join(blk.refs)}: moved {dist:.2f} mm"
              + (f", rotated {v.rotation:g}" if v.rotation else ""), file=sys.stderr)

    if moved:
        pcbnew.SaveBoard(str(board), b)
    nets = _lib.assert_net_table(board)

    # ---- independent check, off the file just written
    vb = pcbnew.LoadBoard(str(board))
    vref = {fp.GetReference(): fp for fp in vb.GetFootprints()}
    block_of = {r: blk.key for blk in blocks for r in blk.refs}
    vc = {r: court(fp) for r, fp in vref.items()}
    vs = {r: sides_of(fp) for r, fp in vref.items()}
    # Everything that is not a footprint courtyard -- keepouts, corridors,
    # fiducial rings -- plus the screw heads, which are bigger than a hole's
    # courtyard.
    obs = Space(outline, gap)
    for cell in space.cells.values():
        for s, rr, owner in cell:
            if owner not in block_of and (owner not in vref or owner in holes):
                obs.add(s, rr, owner)
    problems = []
    for r in free:
        if not inside(vc[r], outline):
            problems.append(f"{r} courtyard leaves the outline")
        for o, oc in vc.items():
            if o == r or block_of.get(o) == block_of[r] or not (vs[r] & vs[o]):
                continue
            if (o in block_of and o < r):
                continue  # each free pair once
            if hits(grow(vc[r], gap), oc):
                problems.append(f"{r} overlaps {o}")
        for owner in obs.blockers(vs[r], vc[r]):
            problems.append(f"{r} is in {owner}")
    for sat, pin, tref, tpin, net in touching:
        a = [p for p in vref[sat].Pads() if p.GetNumber() == dc.pad_number(cfg, sat, pin)]
        z = [p for p in vref[tref].Pads() if p.GetNumber() == dc.pad_number(cfg, tref, tpin)]
        if not any(pa.GetNetname() == net == pz.GetNetname()
                   and hits(mmrect(rect_of(pa.GetBoundingBox())), mmrect(rect_of(pz.GetBoundingBox())))
                   for pa in a for pz in z):
            problems.append(f"{sat}.{pin} no longer touches {tref}.{tpin}")
    if problems:
        _lib.fail("verify: " + "; ".join(sorted(set(problems))[:12]))

    _lib.emit(
        NAME,
        board=str(board),
        fixed=len(fixed),
        blocks=len(blocks),
        multi_part_blocks=[blk.refs for blk in blocks if len(blk.refs) > 1],
        moved=moved,
        max_move_mm=max((m["moved_mm"] for m in moved), default=0.0),
        rotated=rotated,
        connectors_not_guarded=unguarded,
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
