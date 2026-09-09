#!/usr/bin/env python3
"""A* completion router for the airwires the autorouter leaves behind.

Declaring an inner layer a plane costs a routing layer, so freerouting
exhausts its pass budget with a tail of signal nets unrouted — and the
L/Z-link fallback in post_route_fix.py produces working but ugly half-board
detours.  This pass routes every remaining multi-cluster signal net properly:

  * A* over a `[route.completion] grid_mm` grid on each link layer in turn
    (the back layer first, then any routable inner — both nearly empty on a
    board that bans routing on its planes).  Plane layers are never touched.
  * Obstacles: all same-layer copper (inflated by clearance + half-width),
    through-via barrels, through-hole pads, the board's own no-track rule
    areas, the `[[keepouts]]` boxes from board.toml, the clearance-override
    pad rings, and a board-edge margin.
  * Entry: an existing via/segment of the cluster on the target layer when
    available, else a new via ring-searched next to the cluster with a short
    front-layer stub.
  * Width: `[route] power_track_width_mm` for nets matching
    `[nets.power] patterns`, `[route] track_width_mm` otherwise.  A turn
    penalty keeps paths Manhattan-clean.

Run AFTER the router import, BEFORE or AFTER post_route_fix.py — the two are
complementary (this one routes signals properly, that one owns pour
stitching and is the one whose clustering handles T-junctions).  Idempotent
in effect: a net already in one physical cluster is skipped, so a re-run on a
finished board changes nothing.

Exits 1 when nets remain unfinished, with their names in the JSON line —
"nothing left to route" and "gave up on four nets" must not look alike to a
caller.

board.toml keys consumed:
  [layers] copper, planes, front, back
  [nets.power] patterns             fnmatch patterns -> wide track width
  [[keepouts]] name,x1,y1,x2,y2,layers,allow_tracks,allow_vias
                                    the add_keepouts.py schema, absolute mm
  [[zones.pour]] net                pour nets, skipped (post_route_fix owns
  [[route.pours]] net               them) — as is every net that owns a
                                    filled zone on the board itself
  [route] via_size_mm, via_drill_mm, track_width_mm, power_track_width_mm
  [route] fixed_nets                nets owned by a pre-route pass — never
                                    touched here: a thin stub bridged onto a
                                    matched RF trunk wrecks the matching
  [route.completion] grid_mm, clearance_mm, edge_margin_mm, max_pops,
                     total_budget_s, rounds, skip_nets

Not ported:
  * the retirement banner and the revision-history commentary the source
    carried at the top of the file.
  * the antenna-band constant and the module-keepout box, both written as
    board-relative literals, and the RF net names in the skip set.  Declare
    those regions as `[[keepouts]]` and those nets in `[route] fixed_nets`.
  * the geometry-matched fiducial/mounting-hole ring constants (1.0 mm pad ->
    0.6 mm ring, NPTH > 2.5 mm -> 1.9 mm ring).  Rings now come from the
    pads' own clearance overrides — the source noted it matched geometry
    "rather than read from the API (GetLocalClearance signatures vary)", and
    the signature variance is handled with a two-line shim instead.
  * a per-net time budget that the source assigned and never compared
    against; the whole-pass budget is the one that was live.

Generalized beyond the source, deliberately: the board's own no-track/no-via
rule areas are obstacles here.  The source only knew the two keepout regions
it had hardcoded, and a rule that exists in KiCad but never reaches the
router is the exact defect class this pipeline exists to prevent.
"""

from __future__ import annotations

import heapq
import math
import sys
import time
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import board_frame, count_segments, emit, fail, load_config, pass_parser, to_kicad_xy  # noqa: E402

DEFAULT_COPPER = ["F_Cu", "In1_Cu", "In2_Cu", "B_Cu"]

ROUTE_DEFAULTS = {
    "via_size_mm": 0.6,
    "via_drill_mm": 0.3,
    "track_width_mm": 0.2,
    "power_track_width_mm": 0.3,
}
COMPLETION_DEFAULTS = {
    "grid_mm": 0.25,
    "clearance_mm": 0.16,     # obstacle inflation margin
    "edge_margin_mm": 0.5,
    "max_pops": 30000,        # A* node-expansion cap: unreachable goals fail fast
    "total_budget_s": 360.0,  # whole-pass wall clock
    "rounds": 6,              # link attempts per net before giving up
}


def log(*a) -> None:
    print(*a, file=sys.stderr)


def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses (``In1_Cu``) as well as
    KiCad's own (``In1.Cu``).  board.toml is read by both."""
    return name.replace("_Cu", ".Cu").replace("_", ".")


def merged(defaults: dict, section: dict) -> dict:
    p = dict(defaults)
    p.update({k: v for k, v in section.items() if k in defaults})
    return p


def main() -> int:
    ap = pass_parser("finish_routes")
    args = ap.parse_args()
    cfg = load_config(args.config)
    route_cfg = cfg.get("route", {})
    P = merged(ROUTE_DEFAULTS, route_cfg)
    C = merged(COMPLETION_DEFAULTS, route_cfg.get("completion", {}))

    GRID = float(C["grid_mm"])
    CLEAR = float(C["clearance_mm"])
    EDGE_MARGIN = float(C["edge_margin_mm"])
    VIA_D, VIA_DR = float(P["via_size_mm"]), float(P["via_drill_mm"])
    W_SIG, W_PWR = float(P["track_width_mm"]), float(P["power_track_width_mm"])
    MAX_POPS = int(C["max_pops"])
    TOTAL_BUDGET_S = float(C["total_budget_s"])

    power_patterns = list(cfg.get("nets", {}).get("power", {}).get("patterns") or [])
    # Skipped: pour nets (post_route_fix owns those — a pour-net cluster is
    # served by the plane, not by a track), and nets a pre-route pass owns (a
    # thin stub bridged onto a matched RF trunk wrecks the matching).  The
    # pour list is filled in from the board itself below.
    skip = {p["net"] for p in (route_cfg.get("pours") or []) if p.get("net")}
    skip |= {z["net"] for z in (cfg.get("zones", {}).get("pour") or []) if z.get("net")}
    skip |= set(route_cfg.get("fixed_nets") or [])
    skip |= set(route_cfg.get("completion", {}).get("skip_nets") or [])

    def mm(v):
        return pcbnew.FromMM(v)

    def _seg_box_dist(ax, ay, bx, by, x1, y1, x2, y2):
        """Minimum distance from segment AB to the axis-aligned box; 0 if they
        touch or cross. Exact: for a segment and a convex box the minimum is at
        a box corner or a segment end unless the two intersect."""
        def pt_seg(px, py):
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            tt = 0 if ll == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
            return math.hypot(px - (ax + tt * dx), py - (ay + tt * dy))

        def pt_box(px, py):
            return math.hypot(max(x1 - px, 0, px - x2), max(y1 - py, 0, py - y2))

        def inside(px, py):
            return x1 <= px <= x2 and y1 <= py <= y2

        if inside(ax, ay) or inside(bx, by):
            return 0.0
        # segment crossing an edge of the box: cheap Liang-Barsky clip test
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
            if p == 0:
                if q < 0:
                    break
                continue
            r = q / p
            if p < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                break
        else:
            return 0.0
        return min(pt_seg(x1, y1), pt_seg(x2, y1), pt_seg(x1, y2), pt_seg(x2, y2),
                   pt_box(ax, ay), pt_box(bx, by))

    def tm(v):
        return pcbnew.ToMM(int(v))

    pcb_path = Path(args.board)
    segs_before = count_segments(pcb_path)
    b = pcbnew.LoadBoard(str(pcb_path))
    if b is None:
        fail(f"pcbnew could not load {pcb_path} (KiCad major mismatch?)")

    lay_cfg = cfg.get("layers", {})
    copper_names = list(lay_cfg.get("copper") or DEFAULT_COPPER)
    plane_names = list(lay_cfg.get("planes") or [])
    front_name = lay_cfg.get("front", "F_Cu")
    back_name = lay_cfg.get("back", "B_Cu")

    def lid(name):
        i = b.GetLayerID(layer_name(name))
        if i < 0:
            fail(f"layer {layer_name(name)} is not on this board")
        return i

    ids = {n: lid(n) for n in copper_names}
    if front_name not in copper_names or back_name not in copper_names:
        fail(f"[layers] front/back must be in [layers] copper={copper_names}")
    fcu, bcu = ids[front_name], ids[back_name]
    all_cu = [ids[n] for n in copper_names]
    routable = [n for n in copper_names if n not in plane_names]
    if front_name not in routable:
        fail(f"[layers] front={front_name} is declared a plane")
    link_order = ([back_name] if back_name in routable and back_name != front_name else [])
    link_order += [n for n in routable if n not in (front_name, back_name)]
    link_layers = [(ids[n], layer_name(n)) for n in link_order]
    if not link_layers:
        fail("no link layer available — every layer but the front is a plane")

    bb = b.GetBoardEdgesBoundingBox()
    ox, oy = bb.GetLeft(), bb.GetTop()
    W, H = tm(bb.GetWidth()), tm(bb.GetHeight())
    nx, ny = int(W / GRID), int(H / GRID)
    if nx < 3 or ny < 3:
        fail("board edge bounding box is degenerate — is Edge.Cuts drawn?")

    def to_cell(x, y):
        return (min(nx - 1, max(0, int(tm(x - ox) / GRID))),
                min(ny - 1, max(0, int(tm(y - oy) / GRID))))

    def to_abs(cx, cy):
        return (ox + mm((cx + 0.5) * GRID), oy + mm((cy + 0.5) * GRID))

    def is_power(name: str) -> bool:
        return any(fnmatch(name, p) for p in power_patterns)

    def via_radius(v):
        # KiCad 10: PCB_VIA.GetWidth() without a layer argument asserts and
        # returns garbage (padstacks made the width per-layer).
        try:
            return v.GetWidth(fcu) // 2
        except TypeError:
            return v.GetWidth() // 2

    # ---- exclusion boxes: the board's own rule areas + board.toml keepouts --
    # (left, right, top, bottom, notracks, novias, layer ids)
    boxes = []
    for z in b.Zones():
        if z.GetIsRuleArea() and (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias()):
            lset = z.GetLayerSet()
            zb = z.GetBoundingBox()
            boxes.append((zb.GetLeft(), zb.GetRight(), zb.GetTop(), zb.GetBottom(),
                          bool(z.GetDoNotAllowTracks()), bool(z.GetDoNotAllowVias()),
                          {l for l in all_cu if lset.Contains(l)}))
    # The same `[[keepouts]]` schema add_keepouts.py materialises into rule
    # areas, read again so this pass is correct on a board where that pass has
    # not run — and honouring the same `allow_tracks` / `allow_vias` opt-outs,
    # so a keepout that only bans pours does not silently ban routing too.
    ids_by_layer_name = {layer_name(n): i for n, i in ids.items()}
    ko_frame = board_frame(b)
    for ko in cfg.get("keepouts", []):
        try:
            x1, y1, x2, y2 = (float(ko["x1"]), float(ko["y1"]),
                              float(ko["x2"]), float(ko["y2"]))
        except (KeyError, TypeError, ValueError):
            fail(f"[[keepouts]] {ko.get('name', '?')}: needs x1,y1,x2,y2 in mm")
        # config is board-frame (bottom-left, Y-up) — see _lib.board_frame
        x1, y1 = to_kicad_xy(ko_frame, x1, y1)
        x2, y2 = to_kicad_xy(ko_frame, x2, y2)
        names = ko.get("layers") or list(ids_by_layer_name)
        lys = {ids_by_layer_name[layer_name(n)] for n in names
               if layer_name(n) in ids_by_layer_name}
        notracks = not bool(ko.get("allow_tracks", False))
        novias = not bool(ko.get("allow_vias", False))
        if not (notracks or novias):
            continue
        boxes.append((mm(min(x1, x2)), mm(max(x1, x2)), mm(min(y1, y2)), mm(max(y1, y2)),
                      notracks, novias, lys))

    # ---- collect copper ------------------------------------------------------
    # extra = the pad's own clearance override, which the router cannot see in
    # the DSN (Specctra's clearance model is per-netclass) — the same pads
    # export_dsn.py fences off with injected keepouts.
    pads = []      # (x, y, hx, hy, net, layers:set, extra)
    pad_labels = []   # "REF.pad", parallel to pads, for the graze log
    pad_objs = []     # the PAD itself, parallel to pads, for exact-shape checks
    for fp in b.GetFootprints():
        for p in fp.Pads():
            pos = p.GetPosition()
            sz = p.GetSize()
            if p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                lay = set(all_cu)
            else:
                ls = p.GetLayerSet()
                lay = {l for l in all_cu if ls.Contains(l)}
            extra = 0
            if p.GetNetCode() <= 0:
                try:
                    lc = p.GetLocalClearance()
                except TypeError:
                    lc = p.GetLocalClearance(None)
                extra = int(lc or 0)
            # GetSize() is the UNROTATED pad. A rectangular pad on a part at
            # 90/270 has its long axis the other way, and applying the box in
            # the wrong axis is how via_site_ok cleared a via sitting ON C38
            # pad 2 (rot 90) and another ON J8 pad 5 (rot 270): five shorts,
            # five mask bridges and three hole-clearance errors from two
            # escape vias. post_route_fix already swaps the axes; this is the
            # same rule, one pass along.
            #
            # And GetSize() is the ANCHOR of a custom-shaped pad, not its
            # copper: the JST-GH mounting-peg pads are drawn from primitives,
            # so a 2.0mm entry_site stub was cleared against a box far smaller
            # than the peg and landed 0.021mm from it. The bounding box is the
            # copper for every shape, rotated or not, so use that. It is
            # conservative on a 45-degree pad; that is the right direction.
            #
            # The bounding box ALONE. A first version took max(bbox, GetSize())
            # per axis as a belt-and-braces guard, and that put the rotation bug
            # straight back in the other axis: a rotated MSOP pad's sz.x is its
            # unrotated 1.5mm length, so the box came out 3.0mm wide for a pad
            # 0.35mm wide, and every track legitimately passing the pad row
            # read as inside it by up to 0.35mm. Measured: 89 segments stripped,
            # 62 nets unconnected.
            bb = p.GetBoundingBox()
            hx = bb.GetWidth() // 2
            hy = bb.GetHeight() // 2
            cx, cy = bb.GetCenter().x, bb.GetCenter().y
            pads.append((cx, cy, hx + mm(0.1), hy + mm(0.1),
                         p.GetNetCode(), lay, extra))
            pad_labels.append(f"{fp.GetReference()}.{p.GetPadName()}")
            pad_objs.append(p)

    # ---- step 0: strip router copper violating those invisible rings ---------
    ringed = [(x, y, max(hx, hy), lay, extra) for x, y, hx, hy, net, lay, extra in pads
              if extra > 0]
    kill = []
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            r = via_radius(t)
            for x, y, pr, lay, extra in ringed:
                if math.hypot(p.x - x, p.y - y) < pr + extra + r:
                    kill.append(t)
                    break
        else:
            s, e = t.GetStart(), t.GetEnd()
            hw = t.GetWidth() // 2
            for x, y, pr, lay, extra in ringed:
                if t.GetLayer() not in lay:
                    continue
                dx, dy = e.x - s.x, e.y - s.y
                ll = dx * dx + dy * dy
                tt = 0 if ll == 0 else max(0, min(1, ((x - s.x) * dx + (y - s.y) * dy) / ll))
                d = math.hypot(x - (s.x + tt * dx), y - (s.y + tt * dy))
                if d < pr + extra + hw:
                    kill.append(t)
                    break
    n_ring = len(kill)

    # ---- step 0b: strip router segments that graze a foreign via -------------
    # Freerouting lays the odd track 20-90 um closer to a PRE-PLACED via (a
    # fanout escape, a stitching via) than its own rule, and which net it
    # picks moves run to run: CAN4_H, then EVK_5V_IN, then EN_BUCK. A nudge
    # tool cannot fix it -- the shortfall exceeds the nudge budget and the
    # via is fixed by design -- but this pass can: the segment goes, the net
    # falls into the completion set below, and the A* re-lays it through
    # path_ok at the real clearance. Deterministic, and it uses copper this
    # pass already knows how to place. Collected BEFORE any Remove(), like the
    # ring strip above: the wrappers go stale after the first one.
    via_snap = []
    seg_snap = []
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            via_snap.append((p.x, p.y, via_radius(t), t.GetNetCode()))
        elif id(t) not in {id(k) for k in kill}:
            s, e = t.GetStart(), t.GetEnd()
            seg_snap.append((t, s.x, s.y, e.x, e.y, t.GetWidth() // 2,
                             t.GetNetCode()))
    # Foreign PADS too, the same way: the next run's grazes were CAN2_SPLIT
    # against D11 pad 2 and +5V_ARM4 against U17 pad 5, both under 0.15 mm.
    # `pads` carries a +0.1 mm pad here already, so the threshold is lowered
    # by that much -- comparing the padded box against CLEAR would strip
    # legitimate copper at 0.2 mm.
    pad_snap = [(px, py, hx - mm(0.1), hy - mm(0.1), pnet, lay, pad_labels[i], pad_objs[i])
                for i, (px, py, hx, hy, pnet, lay, _extra) in enumerate(pads)
                if pnet > 0]
    grazed = []
    detail = []      # (d_mm, description) for the worst few, so a wrong
                     # geometry model names itself instead of hiding in a count
    for t, x0, y0, x1, y1, hw, nc in seg_snap:
        dx, dy = x1 - x0, y1 - y0
        ll = dx * dx + dy * dy
        hit = None
        for vx, vy, vr, vnc in via_snap:
            if vnc == nc:
                continue
            tt = 0 if ll == 0 else max(0, min(1, ((vx - x0) * dx + (vy - y0) * dy) / ll))
            d = math.hypot(vx - (x0 + tt * dx), vy - (y0 + tt * dy)) - hw - vr
            if d < mm(CLEAR):
                hit = d
                detail.append((tm(d), f"{b.FindNet(nc).GetNetname()} seg on "
                               f"{b.GetLayerName(t.GetLayer())} vs via "
                               f"[{b.FindNet(vnc).GetNetname()}]"))
                break
        if hit is None:
            lay_t = t.GetLayer()
            for px, py, hx, hy, pnet, lay, label, pad_obj in pad_snap:
                if pnet == nc or lay_t not in lay:
                    continue
                # Exact segment-to-box distance. The first version projected
                # the pad CENTRE onto the segment and took a Chebyshev
                # distance from there, which is fine head-on and wrong by up
                # to a factor of two on a 45-degree approach: 74 power tracks
                # at a legal 0.15+ mm from 0402 GND pads read as 0.05 mm and
                # were stripped. Between a segment and an axis-aligned box the
                # minimum is attained at a corner of the box or an end of the
                # segment, unless they intersect, so that is what is measured.
                d = _seg_box_dist(x0, y0, x1, y1, px - hx, py - hy, px + hx, py + hy) - hw
                if d >= mm(CLEAR):
                    continue
                # The box is a PREFILTER. A roundrect pad has no corner where
                # its box does, and on a diagonal approach that is worth
                # r*(sqrt(2)-1): C13.2 measured 0.074 mm to the box corner and
                # 0.159 mm to the copper, which is why DRC passes it and a box
                # test does not. Confirm against pcbnew's own effective shape
                # -- the geometry DRC uses -- so this pass and DRC agree by
                # construction. Only the few box-hits pay for the SWIG call.
                seg_shape = pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(int(x0), int(y0)),
                                                 pcbnew.VECTOR2I(int(x1), int(y1)),
                                                 int(2 * hw))
                if not pad_obj.GetEffectiveShape(lay_t).Collide(seg_shape, int(mm(CLEAR))):
                    continue
                hit = d
                detail.append((tm(d), f"{b.FindNet(nc).GetNetname()} seg on "
                               f"{b.GetLayerName(lay_t)} {tm(x0):.2f},{tm(y0):.2f}->"
                               f"{tm(x1):.2f},{tm(y1):.2f} w{tm(2 * hw):.2f} vs pad "
                               f"{label} [{b.FindNet(pnet).GetNetname()}] box "
                               f"{tm(2 * hx):.2f}x{tm(2 * hy):.2f} at {tm(px):.2f},{tm(py):.2f}"))
                break
        if hit is not None:
            grazed.append((t, tm(hit)))
    kill += [t for t, _d in grazed]

    for t in kill:
        b.Remove(t)
    if n_ring:
        log(f"  stripped {n_ring} router item(s) inside clearance-override pad rings")
    if grazed:
        log(f"  stripped {len(grazed)} router segment(s) grazing a foreign via/pad "
            f"(worst {min(d for _t, d in grazed):.3f} mm vs {CLEAR} mm); "
            f"their nets re-enter the completion set")
        for d, desc in sorted(detail)[:6]:
            log(f"      {d:+.3f} mm  {desc}")

    vias = []      # (x, y, r, net)
    segs = []      # (x0, y0, x1, y1, halfw, net, layer)
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            vias.append((p.x, p.y, via_radius(t), t.GetNetCode()))
        else:
            s, e = t.GetStart(), t.GetEnd()
            segs.append((s.x, s.y, e.x, e.y, t.GetWidth() // 2,
                         t.GetNetCode(), t.GetLayer()))

    # ---- obstacle grids per routable layer ----------------------------------
    # grid[cy*nx+cx] = netcode+2 of sole owner, 1 = hard obstacle, 0 = free
    def blank():
        g = [0] * (nx * ny)
        # board edge margin
        m = int(EDGE_MARGIN / GRID) + 1
        for cy in range(ny):
            for cx in range(nx):
                if cx < m or cy < m or cx >= nx - m or cy >= ny - m:
                    g[cy * nx + cx] = 1
        return g

    def mark_disc(g, x, y, r_mm, code):
        cx0, cy0 = to_cell(x - mm(r_mm), y - mm(r_mm))
        cx1, cy1 = to_cell(x + mm(r_mm), y + mm(r_mm))
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                ax, ay = to_abs(cx, cy)
                if math.hypot(tm(ax - x), tm(ay - y)) <= r_mm:
                    i = cy * nx + cx
                    if g[i] != code:
                        g[i] = 1 if g[i] else code

    def mark_rect(g, x, y, hx, hy, infl_mm, code):
        cx0, cy0 = to_cell(x - hx - mm(infl_mm), y - hy - mm(infl_mm))
        cx1, cy1 = to_cell(x + hx + mm(infl_mm), y + hy + mm(infl_mm))
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                i = cy * nx + cx
                if g[i] != code:
                    g[i] = 1 if g[i] else code

    def mark_seg(g, x0, y0, x1, y1, r_mm, code):
        steps = max(1, int(math.hypot(tm(x1 - x0), tm(y1 - y0)) / (GRID * 0.5)))
        for q in range(steps + 1):
            x = x0 + (x1 - x0) * q // steps
            y = y0 + (y1 - y0) * q // steps
            mark_disc(g, x, y, r_mm, code)

    def build_grid(layer, trace_half_mm):
        g = blank()
        infl = trace_half_mm + CLEAR
        for left, right, top, bottom, notracks, _novias, lys in boxes:
            if not notracks or layer not in lys:
                continue
            cx0, cy0 = to_cell(left, top)
            cx1, cy1 = to_cell(right, bottom)
            for cy in range(cy0, cy1 + 1):
                for cx in range(cx0, cx1 + 1):
                    g[cy * nx + cx] = 1
        for x, y, hx, hy, net, lay, extra in pads:
            if layer in lay:
                mark_rect(g, x, y, hx, hy, infl + tm(extra), net + 2 if not extra else 1)
        for x, y, r, net in vias:
            mark_disc(g, x, y, tm(r) + infl, net + 2)
        for x0, y0, x1, y1, hw, net, lay in segs:
            if lay == layer:
                mark_seg(g, x0, y0, x1, y1, tm(hw) + infl, net + 2)
        return g

    # ---- clustering (loose union-find, generous tolerance) -------------------
    def clusters_for(nc):
        items = []   # [x, y, kind, layers, radius]
        for x, y, hx, hy, net, lay, extra in pads:
            if net == nc:
                items.append([x, y, "pad", lay, max(hx, hy)])
        for x, y, r, net in vias:
            if net == nc:
                items.append([x, y, "via", set(all_cu), r])
        for x0, y0, x1, y1, hw, net, lay in segs:
            if net == nc:
                items.append([x0, y0, "seg", {lay}, hw])
                items.append([x1, y1, "seg", {lay}, hw])
        n = len(items)
        par = list(range(n))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                a, c = items[i], items[j]
                if a[3] & c[3] or a[2] == "via" or c[2] == "via":
                    tol = a[4] + c[4] + mm(0.05)
                    if math.hypot(a[0] - c[0], a[1] - c[1]) <= tol:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            par[ri] = rj
        cl = defaultdict(list)
        for i in range(n):
            cl[find(i)].append(items[i])
        return list(cl.values())

    # ---- primitive emitters (update obstacle sources too) --------------------
    def add_via(x, y, net):
        v = pcbnew.PCB_VIA(b)
        v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        v.SetWidth(mm(VIA_D))
        v.SetDrill(mm(VIA_DR))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(fcu, bcu)
        v.SetNet(net)
        b.Add(v)
        vias.append((int(x), int(y), mm(VIA_D) // 2, net.GetNetCode()))

    def add_track(x0, y0, x1, y1, net, layer, w_mm):
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(int(x0), int(y0)))
        t.SetEnd(pcbnew.VECTOR2I(int(x1), int(y1)))
        t.SetWidth(mm(w_mm))
        t.SetLayer(layer)
        t.SetNet(net)
        b.Add(t)
        segs.append((int(x0), int(y0), int(x1), int(y1), mm(w_mm) // 2,
                     net.GetNetCode(), layer))

    def via_site_ok(x, y, nc):
        r = mm(VIA_D) / 2
        if not (mm(EDGE_MARGIN + 0.3) < x - ox < mm(W - EDGE_MARGIN - 0.3) and
                mm(EDGE_MARGIN + 0.3) < y - oy < mm(H - EDGE_MARGIN - 0.3)):
            return False
        # a through via punches every layer, so any no-via box excludes it
        for left, right, top, bottom, _nt, novias, _lys in boxes:
            if novias and left - r < x < right + r and top - r < y < bottom + r:
                return False
        for px, py, hx, hy, net, lay, extra in pads:
            d = max(abs(x - px) - hx, abs(y - py) - hy)
            if net == nc and not extra:
                if fcu in lay and d < r - mm(0.05):
                    return False
                continue
            if d - r < mm(CLEAR) + extra:
                return False
        for vx, vy, vr, net in vias:
            d = math.hypot(x - vx, y - vy)
            if d - r - vr < (0 if net == nc else mm(CLEAR)):
                return False
        for x0, y0, x1, y1, hw, net, lay in segs:
            if net == nc:
                continue
            # coarse point-seg distance
            dx, dy = x1 - x0, y1 - y0
            ll = dx * dx + dy * dy
            tt = 0 if ll == 0 else max(0, min(1, ((x - x0) * dx + (y - y0) * dy) / ll))
            d = math.hypot(x - (x0 + tt * dx), y - (y0 + tt * dy))
            if d - r - hw < mm(CLEAR):
                return False
        return True

    def stub_ok(x0, y0, x1, y1, nc, hw=None):
        # hw: the stub's real half-width. It used to be a hardcoded 0.1 while
        # a power-net stub is 0.3 wide, so a +5V_ARM4 stub cleared U17.5 by
        # 0.187 mm in this model and 0.137 mm on the board. Pads are checked
        # against pcbnew's effective shape after a cheap box prefilter, same
        # as the graze strip in step 0b: a 30-degree stub past a roundrect
        # pad is exactly the case a box corner gets wrong.
        if hw is None:
            hw = mm(0.1)
        seg_shape = None
        for i, (px, py, hx, hy, net, lay, extra) in enumerate(pads):
            if (net == nc and not extra) or fcu not in lay:
                continue
            d = _seg_box_dist(x0, y0, x1, y1, px - hx, py - hy, px + hx, py + hy)
            if d - hw >= mm(CLEAR) + extra:
                continue
            if extra:
                return False          # clearance-override ring: the box IS the rule
            if seg_shape is None:
                seg_shape = pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(int(x0), int(y0)),
                                                 pcbnew.VECTOR2I(int(x1), int(y1)),
                                                 int(2 * hw))
            if pad_objs[i].GetEffectiveShape(fcu).Collide(seg_shape, int(mm(CLEAR))):
                return False
        for x0s, y0s, x1s, y1s, hw, net, lay in segs:
            if net == nc or lay != fcu:
                continue
            # coarse seg-seg distance via sampling
            for q in range(5):
                sx = x0 + (x1 - x0) * q // 4
                sy = y0 + (y1 - y0) * q // 4
                dx, dy = x1s - x0s, y1s - y0s
                ll = dx * dx + dy * dy
                tt = 0 if ll == 0 else max(0, min(1, ((sx - x0s) * dx + (sy - y0s) * dy) / ll))
                d = math.hypot(sx - (x0s + tt * dx), sy - (y0s + tt * dy))
                if d - mm(0.1) - hw < mm(CLEAR):
                    return False
        return True

    def entry_site(cluster, nc, layer, stub_hw=None):
        """(x, y, new_via?, stub) giving this cluster presence on `layer`."""
        for x, y, kind, lay, r in cluster:
            if kind == "via" or (kind == "seg" and layer in lay):
                return (x, y, False, None)
        anchors = [(x, y) for x, y, kind, lay, r in cluster if kind == "pad"] or \
                  [(x, y) for x, y, kind, lay, r in cluster]
        for ax, ay in anchors:
            for ring in (0.55, 0.8, 1.1, 1.5, 2.0):
                for q in range(12):
                    x = ax + mm(ring) * math.cos(2 * math.pi * q / 12)
                    y = ay + mm(ring) * math.sin(2 * math.pi * q / 12)
                    if via_site_ok(x, y, nc) and stub_ok(ax, ay, x, y, nc, stub_hw):
                        return (int(x), int(y), True, (ax, ay))
        return None

    # ---- A* ------------------------------------------------------------------
    def astar(g, start, goal, nc):
        code = nc + 2
        s = to_cell(*start)
        t = to_cell(*goal)

        def free(cx, cy):
            v = g[cy * nx + cx]
            return v == 0 or v == code

        openh = [(0, 0, s, None)]
        came = {}
        gc = {s: 0}
        DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
        found = False
        pops = 0
        while openh:
            pops += 1
            if pops > MAX_POPS:      # unreachable/hemmed-in goal: give up fast
                return None
            f, cost, cur, prev_dir = heapq.heappop(openh)
            if cur == t:
                found = True
                break
            if cost > gc.get(cur, 1e18):
                continue
            for d in DIRS:
                nxt = (cur[0] + d[0], cur[1] + d[1])
                if not (0 <= nxt[0] < nx and 0 <= nxt[1] < ny):
                    continue
                # never cross foreign copper — via_site_ok guarantees the
                # endpoints sit on free/own cells, so no end-zone override
                if not free(*nxt):
                    continue
                ncst = cost + 1 + (2 if prev_dir and d != prev_dir else 0)
                if ncst < gc.get(nxt, 1e18):
                    gc[nxt] = ncst
                    came[nxt] = (cur, d)
                    h = abs(nxt[0] - t[0]) + abs(nxt[1] - t[1])
                    heapq.heappush(openh, (ncst + h, ncst, nxt, d))
        if not found:
            return None
        path = [t]
        cur = t
        while cur != s:
            cur = came[cur][0]
            path.append(cur)
        path.reverse()
        # collinear merge
        pts = [path[0]]
        for i in range(1, len(path) - 1):
            a, c, d = path[i - 1], path[i], path[i + 1]
            if (c[0] - a[0], c[1] - a[1]) != (d[0] - c[0], d[1] - c[1]):
                pts.append(c)
        pts.append(path[-1])
        return pts

    # ---- main loop -----------------------------------------------------------
    grids = {}   # (layer, width) -> obstacle grid, rebuilt lazily after emits
    dirty = set()

    def get_grid(layer, w):
        key = (layer, w)
        if key not in grids or key in dirty:
            grids[key] = build_grid(layer, w / 2)
            dirty.discard(key)
        return grids[key]

    # The board is the authority on what is poured: any net owning a filled
    # zone is plane-served, whatever board.toml does or does not say.
    skip |= {z.GetNetname() for z in b.Zones() if not z.GetIsRuleArea()}
    skip.discard("")

    todo = []
    for net_i in range(b.GetNetCount()):
        net = b.FindNet(net_i)
        if net is None:
            continue
        name = net.GetNetname()
        if not name or name in skip or name.startswith("unconnected"):
            continue
        cl = clusters_for(net.GetNetCode())
        if len(cl) > 1:
            todo.append((name, net, cl))
    log(f"nets needing completion: {len(todo)}")

    routed = 0
    unfixed = []
    pass_t0 = time.monotonic()
    for name, net, cl in sorted(todo, key=lambda x: len(x[2])):
        if time.monotonic() - pass_t0 > TOTAL_BUDGET_S:
            unfixed.append(name)
            log(f"  {name}: SKIPPED (pass time budget exhausted)")
            continue
        nc = net.GetNetCode()
        w = W_PWR if is_power(name) else W_SIG
        for _round in range(int(C["rounds"])):
            cl = clusters_for(nc)
            if len(cl) <= 1:
                break
            cl.sort(key=len, reverse=True)
            main_c, minor = cl[0], cl[-1]
            done = False
            for layer, lname in link_layers:
                e1 = entry_site(minor, nc, layer, mm(w) / 2)
                e2 = entry_site(main_c, nc, layer, mm(w) / 2)
                if not e1 or not e2:
                    continue
                g = get_grid(layer, w)
                pts = astar(g, (e1[0], e1[1]), (e2[0], e2[1]), nc)
                if pts is None:
                    continue
                apts = [to_abs(cx, cy) for cx, cy in pts]
                apts[0] = (e1[0], e1[1])
                apts[-1] = (e2[0], e2[1])
                for e in (e1, e2):
                    if e[2]:
                        add_via(e[0], e[1], net)
                        if e[3]:
                            # `w`, not W_SIG. This is the pad-to-via escape
                            # stub, and laying it at the SIGNAL width put
                            # 0.200 mm copper on +1V8, +5V_ARM1/3/4,
                            # +5V_EVK_SW and +5V_GPS -- which validate_gerbers
                            # reads straight off the plotted bytes and fails as
                            # "power/width below 0.3mm". It also contradicted
                            # this module's own docstring, which promises
                            # power_track_width_mm for nets matching
                            # [nets.power] patterns.
                            add_track(e[3][0], e[3][1], e[0], e[1], net, fcu, w)
                for q in range(len(apts) - 1):
                    add_track(*apts[q], *apts[q + 1], net, layer, w)
                length = sum(math.hypot(tm(apts[q + 1][0] - apts[q][0]),
                                        tm(apts[q + 1][1] - apts[q][1]))
                             for q in range(len(apts) - 1))
                log(f"  {name}: {lname} {length:.1f}mm ({len(apts) - 1} segs, w={w})")
                dirty.update({(layer, W_SIG), (layer, W_PWR)})
                routed += 1
                done = True
                break
            if not done:
                unfixed.append(name)
                log(f"  {name}: NO PATH FOUND")
                break

    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(pcb_path), b)
    log(f"saved; unfixed: {sorted(set(unfixed)) if unfixed else 'none'}")

    emit(
        "finish_routes",
        board=str(pcb_path),
        nets_needing_completion=len(todo),
        links_routed=routed,
        stripped_ring_items=len(kill),
        unfixed=sorted(set(unfixed)),
        segments_before=segs_before,
        segments_after=count_segments(pcb_path),
    )
    return 1 if unfixed else 0


if __name__ == "__main__":
    raise SystemExit(main())
