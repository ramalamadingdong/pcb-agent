#!/usr/bin/env python3
"""Post-route connectivity finishing — the completion stack.

Freerouting leaves a consistent residue behind on any board that declares an
inner layer a plane:

  * pour-net stitching tracks/vias straddling rule-area keepouts it cannot see
  * sub-0.01 mm orphan track slivers
  * floating pour-net fragments (supply rails, ground islands) that no zone
    fill can reach — a plane fractured into local islands around dense fanout,
    so a naive "add a stitching via" bonds to a dead island
  * occasionally an unrouted signal tail (a track ending short of its pad)

This pass fixes all of it with DRC-grade geometry:

  1. delete keepout-straddling pour-net tracks/vias (plane-served, safe to
     drop) and any copper of any net inside a clearance-override pad's ring
  2. delete orphan slivers (tracks shorter than [route] sliver_mm)
  3. set island-removal ALWAYS on the declared pour zones, refill
  4. geometric cluster analysis per net (union-find over touching copper —
     KiCad's GetConnectedItems traverses net-level, NOT physical clusters,
     and was the source of a 39-junk-via incident; never use it for this):
       - pour nets: floating cluster -> via over *ring-bonded* plane fill
         (8-point test at [route] bond_probe_ring_mm so a thermal-starved
         sliver can't fool it), else a link layer path between existing
         through-vias (the link layers are nearly empty), else a front-layer
         link
       - signal nets: floating cluster -> direct/L-shaped front-layer link to
         the nearest same-net copper, full clearance checked (segment CROSSING
         test included — endpoint-distance-only misses mid-span crossings and
         once shorted one supply rail into another)
  5. refill + save

Run after the SES import.  Idempotent in effect: the strip stages are
unconditional (they re-derive what to remove from the board itself, they
never append), and every link stage fires only on a net whose copper is in
more than one physical cluster — so a re-run on a finished board changes
nothing.  Verify with sampled DRC afterwards (DRC is nondeterministic).

board.toml keys consumed:
  [layers] copper                 all copper layers      (default F/In1/In2/B)
  [layers] planes                 unroutable plane layers
  [layers] front, back            outer layers           (default F_Cu, B_Cu)
  [nets.power] patterns           fnmatch patterns -> wide track width
  [[keepouts]] name,x1,y1,x2,y2,layers,allow_tracks,allow_vias
                                  the add_keepouts.py schema, read again here
                                  and merged with the board's own rule areas
  [[zones.pour]] net              pour nets (the zones.py schema).  Every net
                                  that owns a filled zone on the board counts
                                  as a pour whether or not it is listed.
  [[route.pours]] net             pour net this pass should treat specially
  [[route.pours]] plane_layer     layer whose fill the bond probe tests
                                  (default: that net's zone layer which is a
                                  declared plane, else its first zone layer)
  [[route.pours]] island_removal_layers
                                  layers of that net's zones to force
                                  ISLAND_REMOVAL_MODE_ALWAYS on (default:
                                  all of them, for a net listed here)
  [route] via_size_mm, via_drill_mm, track_width_mm,
          power_track_width_mm, clearance_mm, grid_mm, edge_margin_mm,
          astar_clearance_mm, sliver_mm, micro_bridge_max_mm,
          pour_link_width_mm, pour_stub_width_mm, bond_probe_ring_mm,
          astar_max_pops, astar_multilayer_max_pops, net_budget_s
  [route] fixed_nets              nets owned by a pre-route pass (RF, matched
                                  pairs) — never linked into here
  [route] net_priority            nets to attempt first, in order
  [route] debug_nets              nets to trace candidate selection for

Not ported:
  * the hardcoded antenna-band strip (board top + 8.5 mm) that stripped
    pour-net stubs poking into it, and its all-layer twin inside the A* grid
    builder.  Both are board geometry; declare the band as a `[[keepouts]]`
    box and it is handled by the same layer-aware rule-box path as every
    other keepout.
  * the geometry-matched fiducial (1.0 mm pad -> 0.6 mm ring) and
    mounting-hole (NPTH > 2.5 mm -> 1.9 mm ring) constants.  Rings are now
    read from the pads' own clearance overrides — the same set export_dsn.py
    fences off in the DSN, so the two passes cannot disagree.
  * the hard-first net ordering, which named one differential pair, and the
    debug trace, which named three nets and subtracted a board origin from
    every printed coordinate.  Both are now config lists.
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

EPS = 1000  # 1 um in KiCad nm units

DEFAULT_COPPER = ["F_Cu", "In1_Cu", "In2_Cu", "B_Cu"]

ROUTE_DEFAULTS = {
    "via_size_mm": 0.6,
    "via_drill_mm": 0.3,
    "track_width_mm": 0.2,
    "power_track_width_mm": 0.3,
    "clearance_mm": 0.15,
    "grid_mm": 0.25,
    "edge_margin_mm": 0.7,
    "astar_clearance_mm": 0.18,
    "sliver_mm": 0.02,
    "micro_bridge_max_mm": 1.6,
    "pour_link_width_mm": 0.4,
    "pour_stub_width_mm": 0.25,
    "bond_probe_ring_mm": 0.45,
    "astar_max_pops": 200000,
    "astar_multilayer_max_pops": 1200000,
    "net_budget_s": 180.0,
}

# Candidate caps.  Every link stage scores O(points^2) endpoint pairs and
# tries the cheapest ones; these bound the work, they are not geometry.
MICRO_BRIDGE_ROUNDS = 4
POUR_VIA_RINGS_MM = (0, 0.4, 0.7, 1.0, 1.5, 2.0, 2.5, 3.0)
VIA_SITE_RINGS_MM = (0, 0.4, 0.7, 1.0, 1.4, 1.8, 2.4, 3.0, 3.6, 4.2, 5.0)
POUR_LINK_PAIRS = 300
LINK_LAYER_PAIRS = 160
ASTAR_PAIRS = 10
ASTAR_MULTILAYER_PAIRS = 3
FRONT_LINK_PAIRS = 400


def log(*a) -> None:
    print(*a, file=sys.stderr)


# ---- geometry ---------------------------------------------------------------

def d_pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def orient(ax, ay, bx, by, cx, cy):
    v = (by - ay) * (cx - bx) - (bx - ax) * (cy - by)
    return 0 if v == 0 else (1 if v > 0 else -1)


def segs_intersect(a0, a1, b0, b1):
    return (orient(*a0, *a1, *b0) != orient(*a0, *a1, *b1)
            and orient(*b0, *b1, *a0) != orient(*b0, *b1, *a1))


def d_seg_seg(a0, a1, b0, b1):
    if segs_intersect(a0, a1, b0, b1):
        return 0.0
    return min(d_pt_seg(*a0, *b0, *b1), d_pt_seg(*a1, *b0, *b1),
               d_pt_seg(*b0, *a0, *a1), d_pt_seg(*b1, *a0, *a1))


def d_pt_rect(px, py, cx, cy, hw, hh):
    return math.hypot(max(abs(px - cx) - hw, 0.0), max(abs(py - cy) - hh, 0.0))


# ---- config -----------------------------------------------------------------

def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses (``In1_Cu``) as well as
    KiCad's own (``In1.Cu``).  board.toml is read by both."""
    return name.replace("_Cu", ".Cu").replace("_", ".")


def layer_ids(board, cfg):
    """(front, back, link_layers, name_of, all_copper) from the stackup.

    `back` is the outer bottom layer whatever its type — a through via spans
    the whole stack, so it is the via layer pair even on a board whose back
    copper is a declared plane.
    """
    lay = cfg.get("layers", {})
    copper = list(lay.get("copper") or DEFAULT_COPPER)
    planes = list(lay.get("planes") or [])
    front_name = lay.get("front", "F_Cu")
    back_name = lay.get("back", "B_Cu")
    routable = [n for n in copper if n not in planes]
    if front_name not in routable:
        fail(f"[layers] front={front_name} is not a routable copper layer "
             f"(copper={copper}, planes={planes})")
    if back_name not in copper:
        fail(f"[layers] back={back_name} is not in [layers] copper={copper}")

    def lid(name):
        i = board.GetLayerID(layer_name(name))
        if i < 0:
            fail(f"layer {layer_name(name)} is not on this board")
        return i

    ids = {n: lid(n) for n in copper}
    front, back = ids[front_name], ids[back_name]
    # Link layers: everything routable that is not the front, back first —
    # the back is where the router leaves the most room, inner routable
    # layers after it.
    ordered = ([back_name] if back_name in routable and back_name != front_name else [])
    ordered += [n for n in routable if n not in (front_name, back_name)]
    link_layers = [(ids[n], layer_name(n)) for n in ordered]
    name_of = {ids[n]: layer_name(n) for n in copper}
    plane_ids = {ids[n] for n in planes if n in ids}
    return front, back, link_layers, name_of, [ids[n] for n in copper], plane_ids


def route_params(cfg):
    p = dict(ROUTE_DEFAULTS)
    p.update({k: v for k, v in cfg.get("route", {}).items() if k in ROUTE_DEFAULTS})
    return p


def power_matcher(cfg):
    pats = list(cfg.get("nets", {}).get("power", {}).get("patterns") or [])

    def is_power(name: str) -> bool:
        return any(fnmatch(name, p) for p in pats)

    return is_power


def config_keepouts(cfg, name_of, frame):
    """`[[keepouts]]` boxes as rule boxes.

    The same schema add_keepouts.py materialises into KiCad rule areas, read
    again here so this pass is correct on a board where that pass has not run
    — and honouring the same `allow_tracks` / `allow_vias` opt-outs, so a
    keepout that only bans pours does not silently ban routing too.
    Config is board-frame (bottom-left, Y-up) — see _lib.board_frame.
    """
    FM = pcbnew.FromMM
    ids_by_name = {v: k for k, v in name_of.items()}
    out = []
    for ko in cfg.get("keepouts", []):
        try:
            x1, y1, x2, y2 = (float(ko["x1"]), float(ko["y1"]),
                              float(ko["x2"]), float(ko["y2"]))
        except (KeyError, TypeError, ValueError):
            fail(f"[[keepouts]] {ko.get('name', '?')}: needs x1,y1,x2,y2 in mm")
        kx1, ky1 = to_kicad_xy(frame, x1, y1)
        kx2, ky2 = to_kicad_xy(frame, x2, y2)
        names = ko.get("layers") or list(ids_by_name)
        lys = set()
        for n in names:
            lid = ids_by_name.get(layer_name(n))
            if lid is not None:
                lys.add(lid)
        notracks = not bool(ko.get("allow_tracks", False))
        novias = not bool(ko.get("allow_vias", False))
        if not (notracks or novias):
            continue
        out.append((FM(min(kx1, kx2)), FM(max(kx1, kx2)),
                    FM(min(ky1, ky2)), FM(max(ky1, ky2)), notracks, novias, lys))
    return out


def main() -> int:
    ap = pass_parser("post_route_fix")
    args = ap.parse_args()
    cfg = load_config(args.config)
    P = route_params(cfg)
    is_power = power_matcher(cfg)

    pcb_path = Path(args.board)
    segs_before = count_segments(pcb_path)
    b = pcbnew.LoadBoard(str(pcb_path))
    if b is None:
        fail(f"pcbnew could not load {pcb_path} (KiCad major mismatch?)")
    FM, TM = pcbnew.FromMM, pcbnew.ToMM
    front, back, link_layers, name_of, all_cu, plane_ids = layer_ids(b, cfg)

    VIA_D, VIA_DR = FM(P["via_size_mm"]), FM(P["via_drill_mm"])
    VIA_R = VIA_D // 2
    CLEAR = FM(P["clearance_mm"])
    TRACK_W, POWER_W = FM(P["track_width_mm"]), FM(P["power_track_width_mm"])
    BOND_RING = float(P["bond_probe_ring_mm"])

    # ---- which nets are poured ----------------------------------------------
    # The BOARD is the authority: a net that owns a filled zone is a pour net,
    # and must never be linked as though it were a signal — the plane already
    # serves it, so a track joining two of its clusters is junk copper.
    # board.toml refines rather than declares: `[[zones.pour]]` (the entries
    # zones.py pours from) and `[[route.pours]]` name pour nets whose zone may
    # not exist yet, and `[[route.pours]]` chooses the bond-probe layer and
    # which zones get island removal.
    zone_layers_by_net = defaultdict(list)
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        zone_layers_by_net[z.GetNetname()] += [l for l in all_cu if z.IsOnLayer(l)]
    pour_overrides = {p["net"]: p
                      for p in (cfg.get("route", {}).get("pours") or []) if p.get("net")}
    for nm in pour_overrides:
        if b.FindNet(nm) is None:
            fail(f"[[route.pours]] net={nm!r} is not a net on this board")
    pour_names = set(zone_layers_by_net) | set(pour_overrides)
    pour_names |= {z["net"] for z in (cfg.get("zones", {}).get("pour") or [])
                   if z.get("net") and b.FindNet(z["net"]) is not None}
    pour_names.discard("")
    fixed_nets = set(cfg.get("route", {}).get("fixed_nets") or [])
    net_priority = list(cfg.get("route", {}).get("net_priority") or [])
    debug_nets = set(cfg.get("route", {}).get("debug_nets") or [])

    def via_radius(v):
        # KiCad 10: PCB_VIA.GetWidth() without a layer argument asserts and
        # returns garbage (padstacks made the width per-layer).
        try:
            return v.GetWidth(front) // 2
        except TypeError:
            return v.GetWidth() // 2

    # ---- 1. keepout-straddling pour items + 2. orphan slivers ---------------
    # Layer-aware: a no-tracks rule area only bans tracks on ITS layers.  A
    # full-board no-tracks box on one inner layer, treated layer-blind,
    # matches every track bbox on the board — that is how this pass once
    # deleted every front-layer ground track, pre-routed RF ties included.
    # No-via boxes stay layer-blind: a through via punches every layer.
    rule_boxes = []
    for z in b.Zones():
        if z.GetIsRuleArea() and (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias()):
            lset = z.GetLayerSet()
            lys = {l for l in all_cu if lset.Contains(l)}
            bb = z.GetBoundingBox()
            rule_boxes.append((bb.GetLeft(), bb.GetRight(), bb.GetTop(), bb.GetBottom(),
                               bool(z.GetDoNotAllowTracks()), bool(z.GetDoNotAllowVias()),
                               lys))
    rule_boxes += config_keepouts(cfg, name_of, board_frame(b))

    def net_w(net):
        return POWER_W if is_power(net.GetNetname()) else TRACK_W

    pour_codes = {b.FindNet(nm).GetNetCode() for nm in pour_names
                  if b.FindNet(nm) is not None}

    # Pad clearance-override rings are invisible to the router (Specctra has
    # no per-pad clearance) — strip ANY track/via inside them, whatever the
    # net; the cluster stitcher below re-links what this breaks.  Same pads
    # export_dsn.py fences off, read the same way.
    rings = []
    for fp in b.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() > 0:
                continue
            try:
                lc = p.GetLocalClearance()
            except TypeError:
                lc = p.GetLocalClearance(None)
            if not lc:
                continue
            pos = p.GetPosition()
            sz = max(p.GetSize().x, p.GetSize().y)
            rings.append((int(pos.x), int(pos.y), int(sz // 2), int(lc)))

    kill = []
    for t in b.GetTracks():
        is_via = t.Type() == pcbnew.PCB_VIA_T
        hw = via_radius(t) if is_via else t.GetWidth() // 2
        for rx, ry, rr, rc in rings:
            if is_via:
                d = math.hypot(t.GetPosition().x - rx, t.GetPosition().y - ry)
            else:
                s, e = t.GetStart(), t.GetEnd()
                dx, dy = e.x - s.x, e.y - s.y
                ll = dx * dx + dy * dy
                tt = 0 if ll == 0 else max(0.0, min(1.0, ((rx - s.x) * dx + (ry - s.y) * dy) / ll))
                d = math.hypot(rx - (s.x + tt * dx), ry - (s.y + tt * dy))
            if d < rr + rc + hw:
                kill.append(t)
                break
    if kill:
        log(f"  ring-strip: {len(kill)} item(s) inside clearance-override pad rings")

    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            r = via_radius(t)
            p = t.GetPosition()
            for left, right, top, bottom, _nt, novias, _lys in rule_boxes:
                if novias and (left - r < p.x < right + r
                               and top - r < p.y < bottom + r):
                    if t.GetNetCode() in pour_codes:
                        kill.append(t)
                    break
        else:
            if t.GetLength() < FM(P["sliver_mm"]):
                kill.append(t)
                continue
            s, e = t.GetStart(), t.GetEnd()
            r = t.GetWidth() // 2
            for left, right, top, bottom, notracks, _nv, lys in rule_boxes:
                if not notracks or t.GetLayer() not in lys:
                    continue
                if (min(s.x, e.x) - r < right and max(s.x, e.x) + r > left
                        and min(s.y, e.y) - r < bottom and max(s.y, e.y) + r > top):
                    if t.GetNetCode() in pour_codes and t.GetLayer() == front:
                        kill.append(t)
                    break

    seen_ids = set()
    for t in kill:
        if id(t) in seen_ids:
            continue
        seen_ids.add(id(t))
        b.Remove(t)
    log(f"deleted {len(seen_ids)} keepout-straddling / sliver / ring items")

    # ---- 3. island removal + refill ----------------------------------------
    # Only for nets with a `[[route.pours]]` entry, and only the layers it
    # lists (all of that net's zones when it lists none).  Island removal
    # deletes unconnected fill, which is what makes the bond probe below
    # honest — a probe that lands on an island the fill kept would report a
    # floating cluster as plane-served.  It is opt-in because it is
    # destructive: nothing infers it for a net nobody named.
    island_zones = 0
    for netname, spec in pour_overrides.items():
        want = {layer_name(n) for n in (spec.get("island_removal_layers") or [])}
        want_ids = {b.GetLayerID(n) for n in want} - {-1}
        for z in b.Zones():
            if z.GetIsRuleArea() or z.GetNetname() != netname:
                continue
            if want_ids and not any(z.IsOnLayer(l) for l in want_ids):
                continue
            z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
            island_zones += 1
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())

    # ---- copper inventory ---------------------------------------------------
    pads_all, segs_all, vias_all = [], [], []
    for fp in b.GetFootprints():
        for p in fp.Pads():
            pos = p.GetPosition()
            sz = p.GetSize()
            ang = abs(p.GetOrientationDegrees()) % 180
            hw, hh = (sz.y / 2, sz.x / 2) if abs(ang - 90) < 1 else (sz.x / 2, sz.y / 2)
            pads_all.append([pos.x, pos.y, hw, hh, p.GetNetCode(), p.HasHole(),
                             fp.GetReference() + "." + p.GetNumber()])
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            vias_all.append([p.x, p.y, via_radius(t), t.GetNetCode()])
        else:
            s, e = t.GetStart(), t.GetEnd()
            segs_all.append([s.x, s.y, e.x, e.y, t.GetWidth() // 2, t.GetNetCode(), t.GetLayer()])

    zones_by_net = defaultdict(list)
    for z in b.Zones():
        if not z.GetIsRuleArea():
            zones_by_net[z.GetNetname()].append(z)

    def covered_ring(zl, layer, x, y, ring=BOND_RING):
        for k in range(8):
            a = 2 * math.pi * k / 8
            px = int(x + FM(ring) * math.cos(a))
            py = int(y + FM(ring) * math.sin(a))
            if not any(z.HitTestFilledArea(layer, pcbnew.VECTOR2I(px, py), 0) for z in zl):
                return False
        return True

    def clusters_for(netcode):
        segs = [s for s in segs_all if s[5] == netcode]
        vias = [v for v in vias_all if v[3] == netcode]
        pads = [p for p in pads_all if p[4] == netcode]
        n = len(segs) + len(vias) + len(pads)
        par = list(range(n))

        def find(x):
            while par[x] != x:
                par[x] = par[par[x]]
                x = par[x]
            return x

        def uni(a, c):
            ra, rb = find(a), find(c)
            if ra != rb:
                par[ra] = rb

        S, V, P_ = 0, len(segs), len(segs) + len(vias)
        for i in range(len(segs)):
            s1 = segs[i]
            for j in range(i + 1, len(segs)):
                s2 = segs[j]
                if s1[6] == s2[6] and d_seg_seg((s1[0], s1[1]), (s1[2], s1[3]),
                                                (s2[0], s2[1]), (s2[2], s2[3])) <= s1[4] + s2[4] + EPS:
                    uni(S + i, S + j)
            for j, v in enumerate(vias):
                if d_pt_seg(v[0], v[1], s1[0], s1[1], s1[2], s1[3]) <= v[2] + s1[4] + EPS:
                    uni(S + i, V + j)
            for j, p in enumerate(pads):
                if not p[5] and s1[6] != front:
                    continue
                if (d_pt_rect(s1[0], s1[1], p[0], p[1], p[2], p[3]) <= s1[4] + EPS
                        or d_pt_rect(s1[2], s1[3], p[0], p[1], p[2], p[3]) <= s1[4] + EPS):
                    uni(S + i, P_ + j)
        for i, v1 in enumerate(vias):
            for j in range(i + 1, len(vias)):
                v2 = vias[j]
                if math.hypot(v1[0] - v2[0], v1[1] - v2[1]) <= v1[2] + v2[2] + EPS:
                    uni(V + i, V + j)
            for j, p in enumerate(pads):
                if d_pt_rect(v1[0], v1[1], p[0], p[1], p[2], p[3]) <= v1[2] + EPS:
                    uni(V + i, P_ + j)
        cl = defaultdict(lambda: {"segs": [], "vias": [], "pads": []})
        for i, s in enumerate(segs):
            cl[find(S + i)]["segs"].append(s)
        for i, v in enumerate(vias):
            cl[find(V + i)]["vias"].append(v)
        for i, p in enumerate(pads):
            cl[find(P_ + i)]["pads"].append(p)
        return cl

    def via_ok(x, y, netcode):
        r = VIA_D / 2
        for p in pads_all:
            d = d_pt_rect(x, y, p[0], p[1], p[2], p[3])
            if p[4] == netcode:
                if p[5] and d < r + FM(0.1):
                    return False
                continue
            if d - r < CLEAR:
                return False
        for v in vias_all:
            d = math.hypot(x - v[0], y - v[1])
            if v[3] == netcode:
                if d < r + v[2]:
                    return False
                continue
            if d - r - v[2] < CLEAR:
                return False
        for s in segs_all:
            if s[5] == netcode:
                continue
            if d_pt_seg(x, y, s[0], s[1], s[2], s[3]) - r - s[4] < CLEAR:
                return False
        # rule areas: no new vias inside a no-via box
        for left, right, top, bottom, _nt, novias, _lys in rule_boxes:
            if novias and left - r < x < right + r and top - r < y < bottom + r:
                return False
        return True

    def path_ok(path, netcode, layer, width):
        hw = width / 2
        # clearance-override pad rings (any net) — the A* grids block these
        # but the L/Z validator must too, or lazy links get laid through them
        for rx, ry, rr, rc in rings:
            for q in range(len(path) - 1):
                (x0, y0), (x1, y1) = path[q], path[q + 1]
                dx, dy = x1 - x0, y1 - y0
                ll = dx * dx + dy * dy
                tt2 = 0 if ll == 0 else max(0.0, min(1.0, ((rx - x0) * dx + (ry - y0) * dy) / ll))
                if math.hypot(rx - (x0 + tt2 * dx), ry - (y0 + tt2 * dy)) < rr + rc + hw:
                    return False
        # no-track rule areas on this layer (bbox test — rule areas are boxes)
        for left, right, top, bottom, notracks, _nv, lys in rule_boxes:
            if not notracks or layer not in lys:
                continue
            for q in range(len(path) - 1):
                (x0, y0), (x1, y1) = path[q], path[q + 1]
                if (min(x0, x1) - hw < right and max(x0, x1) + hw > left
                        and min(y0, y1) - hw < bottom and max(y0, y1) + hw > top):
                    return False
        for q in range(len(path) - 1):
            a0, a1 = path[q], path[q + 1]
            for p in pads_all:
                if p[4] == netcode:
                    continue
                if not p[5] and layer != front:
                    continue
                if d_pt_seg(p[0], p[1], *a0, *a1) - max(p[2], p[3]) - width / 2 < CLEAR:
                    return False
            for v in vias_all:
                if v[3] == netcode:
                    continue
                if d_pt_seg(v[0], v[1], *a0, *a1) - width / 2 - v[2] < CLEAR:
                    return False
            for s in segs_all:
                if s[6] != layer:
                    continue
                if s[5] == netcode:
                    if segs_intersect(a0, a1, (s[0], s[1]), (s[2], s[3])):
                        ends = (path[0], path[-1])
                        if not any(d_pt_seg(*e, s[0], s[1], s[2], s[3]) <= width / 2 + EPS for e in ends):
                            return False
                    continue
                if d_seg_seg(a0, a1, (s[0], s[1]), (s[2], s[3])) - width / 2 - s[4] < CLEAR:
                    return False
        return True

    stats = {"micro_bridges": 0, "pour_vias": 0, "pour_links": 0,
             "front_links": 0, "link_layer_links": 0, "astar_links": 0,
             "astar_multilayer_links": 0, "vias_added": 0}

    def add_via(x, y, net):
        v = pcbnew.PCB_VIA(b)
        v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        v.SetWidth(VIA_D)
        v.SetDrill(VIA_DR)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(front, back)
        v.SetNet(net)
        b.Add(v)
        vias_all.append([int(x), int(y), VIA_R, net.GetNetCode()])
        stats["vias_added"] += 1

    def add_track(x0, y0, x1, y1, net, layer, width):
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(int(x0), int(y0)))
        t.SetEnd(pcbnew.VECTOR2I(int(x1), int(y1)))
        t.SetWidth(int(width))
        t.SetLayer(layer)
        t.SetNet(net)
        b.Add(t)
        segs_all.append([int(x0), int(y0), int(x1), int(y1), int(width) // 2,
                         net.GetNetCode(), layer])

    def cluster_points(c, layer=None):
        pts = []
        for s in c["segs"]:
            if layer is None or s[6] == layer:
                pts += [(s[0], s[1]), ((s[0] + s[2]) // 2, (s[1] + s[3]) // 2), (s[2], s[3])]
        for v in c["vias"]:
            pts.append((v[0], v[1]))
        for p in c["pads"]:
            pts.append((p[0], p[1]))
        return pts

    # ---- 4a. micro-gap bridges ----------------------------------------------
    # Freerouting leaves sub-mm gaps (a track end short of its pad or of
    # another same-net track).  Bridge nearest points on the front layer with
    # one checked segment before any heavier strategy runs.  All nets,
    # including pours.
    for net_i in range(b.GetNetCount()):
        net = b.FindNet(net_i)
        if net is None:
            continue
        nm = net.GetNetname()
        if not nm or nm.startswith("unconnected"):
            continue
        nc = net.GetNetCode()
        for _ in range(MICRO_BRIDGE_ROUNDS):
            cl = clusters_for(nc)
            if len(cl) < 2:
                break
            cl_list = sorted(cl.values(), key=len, reverse=True)
            main_pts = cluster_points(cl_list[0], front)
            bridged = False
            for c in cl_list[1:]:
                cpts = cluster_points(c, front)
                best = None
                for cx, cy in cpts:
                    for mx, my in main_pts:
                        d = math.hypot(cx - mx, cy - my)
                        if best is None or d < best[0]:
                            best = (d, (cx, cy), (mx, my))
                if best and best[0] < FM(P["micro_bridge_max_mm"]):
                    a, bpt = best[1], best[2]
                    if path_ok([a, bpt], nc, front, net_w(net)):
                        add_track(*a, *bpt, net, front, net_w(net))
                        log(f"{nm}: micro-bridge {TM(int(best[0])):.2f}mm")
                        stats["micro_bridges"] += 1
                        bridged = True
                        break
            if not bridged:
                break

    # ---- 4b. pour nets: bond every floating cluster to the plane ------------
    def is_bonded(c, zl, layer):
        for v in c["vias"]:
            if covered_ring(zl, layer, v[0], v[1]):
                return True
        for p in c["pads"]:
            if p[5] and covered_ring(zl, layer, p[0], p[1]):
                return True
        return False

    unfixed = []
    pour_plane_layer = {}
    for netname in sorted(pour_names):
        net = b.FindNet(netname)
        if net is None:
            continue
        zl = zones_by_net.get(netname, [])
        if not zl:
            log(f"{netname}: declared a pour but owns no filled zone — "
                f"nothing to bond floating clusters to")
            continue
        spec = pour_overrides.get(netname, {})
        if spec.get("plane_layer"):
            plane_layer = b.GetLayerID(layer_name(spec["plane_layer"]))
            if plane_layer < 0:
                fail(f"[[route.pours]] net={netname}: plane_layer="
                     f"{spec['plane_layer']!r} is not a layer on this board")
        else:
            # Default probe layer: whichever of this net's zone layers is a
            # declared plane — that is the fill that serves the net board-wide
            # — else its first zone layer.
            zls = zone_layers_by_net.get(netname) or []
            cand = [l for l in zls if l in plane_ids] or zls
            if not cand:
                continue
            plane_layer = cand[0]
        pour_plane_layer[netname] = plane_layer
        nc = net.GetNetCode()
        cl = clusters_for(nc)
        bonded = {k for k, c in cl.items() if is_bonded(c, zl, plane_layer)}
        floating = [k for k in cl if k not in bonded]
        if len(cl) > 1 and not bonded:
            log(f"{netname}: WARNING no plane-bonded cluster found")
        main_vias = [(v[0], v[1]) for k in bonded for v in cl[k]["vias"]]
        for k in floating:
            c = cl[k]
            names = [p[6] for p in c["pads"]][:4]
            done = False
            # (a) via over ring-bonded plane
            for ring in POUR_VIA_RINGS_MM:
                for cx, cy in cluster_points(c, front):
                    offs = [(0, 0)] if ring == 0 else [
                        (FM(ring) * math.cos(2 * math.pi * q / 24),
                         FM(ring) * math.sin(2 * math.pi * q / 24)) for q in range(24)]
                    for ox, oy in offs:
                        x, y = cx + ox, cy + oy
                        if not covered_ring(zl, plane_layer, x, y):
                            continue
                        path = [(cx, cy), (int(x), int(y))] if (ox, oy) != (0, 0) else None
                        if not via_ok(x, y, nc):
                            continue
                        if path and not path_ok(path, nc, front, FM(P["pour_stub_width_mm"])):
                            continue
                        add_via(x, y, net)
                        if path:
                            add_track(cx, cy, x, y, net, front, FM(P["pour_stub_width_mm"]))
                        log(f"{netname} {names}: plane via ({TM(int(x)):.2f},{TM(int(y)):.2f})")
                        stats["pour_vias"] += 1
                        done = True
                        break
                    if done:
                        break
                if done:
                    break
            if done:
                continue
            # (b) link-layer path between vias
            fvias = [(v[0], v[1]) for v in c["vias"]]
            scored = sorted((math.hypot(fx - mx, fy - my), (fx, fy), (mx, my))
                            for fx, fy in fvias for mx, my in main_vias)
            for dist, (fx, fy), (mx, my) in scored[:POUR_LINK_PAIRS]:
                midx, midy = (fx + mx) // 2, (fy + my) // 2
                for layer, lname in link_layers:
                    for path in ([(fx, fy), (mx, my)],
                                 [(fx, fy), (mx, fy), (mx, my)],
                                 [(fx, fy), (fx, my), (mx, my)],
                                 [(fx, fy), (midx, fy), (midx, my), (mx, my)]):
                        if path_ok(path, nc, layer, FM(P["pour_link_width_mm"])):
                            for q in range(len(path) - 1):
                                add_track(*path[q], *path[q + 1], net, layer,
                                          FM(P["pour_link_width_mm"]))
                            log(f"{netname} {names}: {lname} link {TM(int(dist)):.2f}mm")
                            stats["pour_links"] += 1
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
            if not done:
                log(f"{netname} {names}: UNFIXED — manual routing needed")
                unfixed.append(netname)

    # ---- 4b-2. pour islands: a filled region holding no via is unconnected ---
    # DRC's unconnected list for a pour net names ZONE REGIONS, not pads. An
    # F.Cu GND island that touches no via is a separate region of the same net
    # no matter how many pads sit in it, and 4b above cannot see it: clusters_for
    # is built from pads, vias and tracks, so an island containing none of them
    # is not a cluster and is never considered, while one containing only pads
    # reaches stage (b) with an empty via list and falls straight through to
    # UNFIXED. Measured on this board: GND fills into 43 islands on F.Cu and 15
    # on B.Cu against a single clean In1 region.
    #
    # A through via spans F..B, so ONE via inside an island bonds it to the
    # plane and thereby to every other island. That is the whole repair: find
    # islands with no via of their own and give them one.
    #
    # Islands with no pad either should already be gone -- that is what
    # [[route.pours]] island_removal_layers is for -- so what survives here is
    # real copper serving real pads.
    island_vias = island_skipped = 0
    for netname in sorted(pour_names):
        net = b.FindNet(netname)
        plane_layer = pour_plane_layer.get(netname)
        if net is None or plane_layer is None:
            continue
        nc = net.GetNetCode()
        zl = zones_by_net.get(netname, [])
        for z in zl:
            for layer in z.GetLayerSet().CuStack():
                poly = z.GetFilledPolysList(layer)
                for i in range(poly.OutlineCount()):
                    if any(poly.Contains(pcbnew.VECTOR2I(v[0], v[1]), i)
                           for v in vias_all if v[3] == nc):
                        continue
                    outline = poly.Outline(i)
                    pts = [outline.CPoint(j) for j in range(outline.PointCount())]
                    if not pts:
                        continue
                    x0 = min(p.x for p in pts)
                    x1 = max(p.x for p in pts)
                    y0 = min(p.y for p in pts)
                    y1 = max(p.y for p in pts)
                    if min(x1 - x0, y1 - y0) < VIA_D + 2 * CLEAR:
                        island_skipped += 1      # a sliver cannot hold a via
                        continue
                    # Work outward from the middle: the centre of an island has
                    # the most room, and a via there is the least likely to
                    # squeeze a neighbouring net.
                    step = max(FM(0.25), (min(x1 - x0, y1 - y0)) // 8)
                    cxs = sorted(range(x0 + step, x1, step),
                                 key=lambda v: abs(v - (x0 + x1) // 2))
                    cys = sorted(range(y0 + step, y1, step),
                                 key=lambda v: abs(v - (y0 + y1) // 2))
                    inside = [(cx, cy) for cx in cxs[:24] for cy in cys[:24]
                              if poly.Contains(pcbnew.VECTOR2I(int(cx), int(cy)), i)]
                    placed = False
                    # (a) a via wholly inside the island
                    for cx, cy in inside:
                        if not covered_ring(zl, plane_layer, cx, cy):
                            continue
                        if not via_ok(cx, cy, nc):
                            continue
                        add_via(cx, cy, net)
                        island_vias += 1
                        placed = True
                        break
                    # (b) a via OUTSIDE it, reached by a short stub. A 1.1 x 0.9
                    # mm pocket holding a pad has no room for a 0.6 mm via plus
                    # clearance, but the main pour is usually a fraction of a
                    # millimetre away. Same offset-and-stub shape stage (a)
                    # above uses for floating clusters.
                    if not placed:
                        for ring in POUR_VIA_RINGS_MM[1:]:
                            for cx, cy in inside:
                                for q in range(24):
                                    ox = FM(ring) * math.cos(2 * math.pi * q / 24)
                                    oy = FM(ring) * math.sin(2 * math.pi * q / 24)
                                    x, y = int(cx + ox), int(cy + oy)
                                    if not covered_ring(zl, plane_layer, x, y):
                                        continue
                                    if not via_ok(x, y, nc):
                                        continue
                                    stub = [(cx, cy), (x, y)]
                                    if not path_ok(stub, nc, layer,
                                                   FM(P["pour_stub_width_mm"])):
                                        continue
                                    add_via(x, y, net)
                                    add_track(cx, cy, x, y, net, layer,
                                              FM(P["pour_stub_width_mm"]))
                                    island_vias += 1
                                    placed = True
                                    break
                                if placed:
                                    break
                            if placed:
                                break
                    if not placed:
                        island_skipped += 1
                        log(f"{netname}: island on {b.GetLayerName(layer)} at "
                            f"({TM(x0):.1f},{TM(y0):.1f})-({TM(x1):.1f},"
                            f"{TM(y1):.1f}) has no via and no legal site for one")
    if island_vias or island_skipped:
        log(f"pour islands stitched: {island_vias} via(s) added, "
            f"{island_skipped} island(s) left (slivers or no legal site)")
    stats["island_vias"] = island_vias

    # ---- signal-net link-layer fallback helpers -----------------------------
    # The link layers are nearly empty on a board that bans routing on its
    # planes — the router's congestion lands as unrouted tails on the front
    # layer instead.  When no front-layer link clears, drop a via next to each
    # stranded cluster and run the link across a link layer.  All geometry
    # goes through via_ok/path_ok, which also reject the no-track/no-via rule
    # areas per layer.
    def ring_sites(x, y, ring_mm):
        if ring_mm == 0:
            return [(int(x), int(y))]
        return [(int(x + FM(ring_mm) * math.cos(2 * math.pi * q / 12)),
                 int(y + FM(ring_mm) * math.sin(2 * math.pi * q / 12)))
                for q in range(12)]

    def find_via_site(x, y, nc):
        """A via location at/near (x,y) plus the front-layer stub to reach it."""
        for ring in VIA_SITE_RINGS_MM:
            for vx, vy in ring_sites(x, y, ring):
                if not via_ok(vx, vy, nc):
                    continue
                stub = None if (vx, vy) == (int(x), int(y)) else [(int(x), int(y)), (vx, vy)]
                if stub and not path_ok(stub, nc, front, TRACK_W):
                    continue
                return (vx, vy), stub
        return None, None

    def layer_link(c, main_pts, net, nc, names):
        # Link layers only: the planes are routing-banned, and a signal laid
        # on a plane is the defect the plane declaration exists to prevent.
        scored = sorted((math.hypot(cx - mx, cy - my), (cx, cy), (mx, my))
                        for cx, cy in cluster_points(c, front) for mx, my in main_pts)
        for dist, (cx, cy), (mx, my) in scored[:LINK_LAYER_PAIRS]:
            va, stub_a = find_via_site(cx, cy, nc)
            if va is None:
                continue
            vb, stub_b = find_via_site(mx, my, nc)
            if vb is None:
                continue
            midx, midy = (va[0] + vb[0]) // 2, (va[1] + vb[1]) // 2
            for layer, lname in link_layers:
                for path in ([va, vb],
                             [va, (vb[0], va[1]), vb],
                             [va, (va[0], vb[1]), vb],
                             [va, (midx, va[1]), (midx, vb[1]), vb]):
                    if not path_ok(path, nc, layer, net_w(net)):
                        continue
                    add_via(*va, net)
                    add_via(*vb, net)
                    if stub_a:
                        add_track(*stub_a[0], *stub_a[1], net, front, net_w(net))
                    if stub_b:
                        add_track(*stub_b[0], *stub_b[1], net, front, net_w(net))
                    for q in range(len(path) - 1):
                        add_track(*path[q], *path[q + 1], net, layer, net_w(net))
                    log(f"{net.GetNetname()} {names}: {lname} signal link {TM(int(dist)):.2f}mm")
                    stats["link_layer_links"] += 1
                    return True
        return False

    # ---- grid A* fallback ----------------------------------------------------
    # Real pathfinding on a link layer when the L/Z shapes cannot clear.  This
    # lives HERE (not in the completion pass) because THIS file's union-find
    # clustering handles T-junctions correctly — the A* only ever sees
    # genuinely disconnected clusters.  Obstacles: all foreign copper,
    # through-hole pads, no-track rule areas, clearance-override pad rings,
    # board margin.  Caps: node pops, candidate site pairs, per-net wall clock.
    GRIDA = FM(P["grid_mm"])
    _bbx = b.GetBoardEdgesBoundingBox()
    _gox, _goy = _bbx.GetLeft(), _bbx.GetTop()
    _gnx, _gny = int(_bbx.GetWidth() / GRIDA), int(_bbx.GetHeight() / GRIDA)
    if _gnx < 3 or _gny < 3:
        fail("board edge bounding box is degenerate — is Edge.Cuts drawn?")

    def _blk_rect(g, x, y, rx, ry):
        c0x = max(0, int((x - rx - _gox) / GRIDA))
        c1x = min(_gnx - 1, int((x + rx - _gox) / GRIDA))
        c0y = max(0, int((y - ry - _goy) / GRIDA))
        c1y = min(_gny - 1, int((y + ry - _goy) / GRIDA))
        for cy in range(c0y, c1y + 1):
            row = cy * _gnx
            for cx in range(c0x, c1x + 1):
                g[row + cx] = 1

    def _blk_disc(g, x, y, r):
        _blk_rect(g, x, y, r, r)

    def build_astar_grid(layer, nc, width):
        g = bytearray(_gnx * _gny)
        m = int(FM(P["edge_margin_mm"]) / GRIDA) + 1
        for cy in range(_gny):
            row = cy * _gnx
            if cy < m or cy >= _gny - m:
                for cx in range(_gnx):
                    g[row + cx] = 1
            else:
                for cx in range(m):
                    g[row + cx] = 1
                for cx in range(_gnx - m, _gnx):
                    g[row + cx] = 1
        infl = width // 2 + FM(P["astar_clearance_mm"])
        for x0, y0, x1, y1, hw, snet, slay in segs_all:
            if slay != layer or snet == nc:
                continue
            steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / (GRIDA * 0.6)))
            for q in range(steps + 1):
                _blk_disc(g, x0 + (x1 - x0) * q // steps,
                          y0 + (y1 - y0) * q // steps, hw + infl)
        for x, y, r, vnet in vias_all:
            if vnet != nc:
                _blk_disc(g, x, y, r + infl)
        for p in pads_all:
            if p[4] == nc:
                continue
            if not p[5] and layer != front:   # SMD pads block only their own layer
                continue
            # rectangular inflation — a disc of max(hx,hy) fuses elongated
            # pad rows (module castellations) into a solid wall that seals the
            # stub endpoints the A* needs to escape from
            _blk_rect(g, p[0], p[1], p[2] + infl, p[3] + infl)
        for left, right, top, bottom, notracks, _nv, lys in rule_boxes:
            if not notracks or layer not in lys:
                continue
            c0x = max(0, int((left - _gox) / GRIDA))
            c1x = min(_gnx - 1, int((right - _gox) / GRIDA))
            c0y = max(0, int((top - _goy) / GRIDA))
            c1y = min(_gny - 1, int((bottom - _goy) / GRIDA))
            for cy in range(c0y, c1y + 1):
                row = cy * _gnx
                for cx in range(c0x, c1x + 1):
                    g[row + cx] = 1
        for rx, ry, rr, rc in rings:
            _blk_disc(g, rx, ry, rr + rc + infl)
        return g

    def astar_grid(g, s_pt, t_pt):
        def cell(p):
            return (min(_gnx - 1, max(0, int((p[0] - _gox) / GRIDA))),
                    min(_gny - 1, max(0, int((p[1] - _goy) / GRIDA))))
        s, tt = cell(s_pt), cell(t_pt)
        free_override = {s, tt}
        openh = [(0, 0, s, None)]
        came = {}
        gc = {s: 0}
        pops = 0
        DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
        while openh:
            pops += 1
            if pops > P["astar_max_pops"]:
                return None
            f, cost, cur, pdir = heapq.heappop(openh)
            if cur == tt:
                path = [cur]
                while cur != s:
                    cur = came[cur][0]
                    path.append(cur)
                path.reverse()
                pts = [path[0]]
                for i in range(1, len(path) - 1):
                    a, c2, d2 = path[i - 1], path[i], path[i + 1]
                    if (c2[0] - a[0], c2[1] - a[1]) != (d2[0] - c2[0], d2[1] - c2[1]):
                        pts.append(c2)
                pts.append(path[-1])
                out = [(int(_gox + (cx + 0.5) * GRIDA), int(_goy + (cy + 0.5) * GRIDA))
                       for cx, cy in pts]
                out[0] = (int(s_pt[0]), int(s_pt[1]))
                out[-1] = (int(t_pt[0]), int(t_pt[1]))
                return out
            if cost > gc.get(cur, 1 << 60):
                continue
            for d in DIRS:
                nxt = (cur[0] + d[0], cur[1] + d[1])
                if not (0 <= nxt[0] < _gnx and 0 <= nxt[1] < _gny):
                    continue
                if g[nxt[1] * _gnx + nxt[0]] and nxt not in free_override:
                    continue
                ncst = cost + 1 + (2 if pdir and d != pdir else 0)
                if ncst < gc.get(nxt, 1 << 60):
                    gc[nxt] = ncst
                    came[nxt] = (cur, d)
                    heapq.heappush(
                        openh,
                        (ncst + abs(nxt[0] - tt[0]) + abs(nxt[1] - tt[1]), ncst, nxt, d))
        return None

    def astar_link(c, main_pts, net, nc, names):
        t0 = time.monotonic()
        width = POWER_W if is_power(net.GetNetname()) else TRACK_W
        scored = sorted((math.hypot(cx - mx, cy - my), (cx, cy), (mx, my))
                        for cx, cy in cluster_points(c, front) for mx, my in main_pts)
        dbg = net.GetNetname() in debug_nets
        _gcache = {}
        for dist, (cx, cy), (mx, my) in scored[:ASTAR_PAIRS]:
            if time.monotonic() - t0 > P["net_budget_s"]:
                break
            va, stub_a = find_via_site(cx, cy, nc)
            vb, stub_b = find_via_site(mx, my, nc)
            if dbg:
                log(f"    dbg {net.GetNetname()}: pair d={TM(int(dist)):.1f} "
                    f"a=({TM(int(cx)):.1f},{TM(int(cy)):.1f})->{'OK' if va else 'NOSITE'} "
                    f"b=({TM(int(mx)):.1f},{TM(int(my)):.1f})->{'OK' if vb else 'NOSITE'}")
            if va is None or vb is None:
                continue
            for layer, lname in link_layers:
                if layer not in _gcache:
                    _gcache[layer] = build_astar_grid(layer, nc, width)
                g = _gcache[layer]
                pts = astar_grid(g, va, vb)
                if pts is None:
                    if dbg:
                        log(f"    dbg {net.GetNetname()}: A* no path on {lname}")
                    continue
                add_via(*va, net)
                add_via(*vb, net)
                if stub_a:
                    add_track(*stub_a[0], *stub_a[1], net, front, TRACK_W)
                if stub_b:
                    add_track(*stub_b[0], *stub_b[1], net, front, TRACK_W)
                for q in range(len(pts) - 1):
                    add_track(*pts[q], *pts[q + 1], net, layer, width)
                length = sum(math.hypot(pts[q + 1][0] - pts[q][0],
                                        pts[q + 1][1] - pts[q][1])
                             for q in range(len(pts) - 1)) / 1e6
                log(f"{net.GetNetname()} {names}: A* {lname} {length:.1f}mm "
                    f"({len(pts) - 1} segs)")
                stats["astar_links"] += 1
                return True
        return False

    def astar_multilayer_link(c, main_pts, net, nc, names):
        """Multi-layer A* (front + link layers, with via transitions), pad-to-pad.

        For endpoints whose link-layer neighbourhoods are sealed: starts ON
        the front-layer copper itself, escapes through front-layer lanes, and
        dives wherever a via actually fits.  Via-legal cells are approximated
        as free on every layer grid; each emitted via is re-validated with
        via_ok.  Plane layers are never in the grid set.
        """
        width = POWER_W if is_power(net.GetNetname()) else TRACK_W
        LAYS = [front] + [l for l, _n in link_layers]
        grids = [build_astar_grid(l, nc, width) for l in LAYS]
        scored = sorted((math.hypot(cx - mx, cy - my), (cx, cy), (mx, my))
                        for cx, cy in cluster_points(c, front) for mx, my in main_pts)
        if not scored:
            return False
        for dist0, (sx, sy), (tx, ty) in scored[:ASTAR_MULTILAYER_PAIRS]:
            if _try_astar_multilayer(net, nc, names, width, grids, LAYS, sx, sy, tx, ty):
                return True
        return False

    def _try_astar_multilayer(net, nc, names, width, grids, LAYS, sx, sy, tx, ty):
        def cell(p):
            return (min(_gnx - 1, max(0, int((p[0] - _gox) / GRIDA))),
                    min(_gny - 1, max(0, int((p[1] - _goy) / GRIDA))))

        s = cell((sx, sy)) + (0,)
        tt = cell((tx, ty)) + (0,)
        override = {(s[0], s[1], 0), (tt[0], tt[1], 0)}

        def free(cx, cy, li):
            return grids[li][cy * _gnx + cx] == 0 or (cx, cy, li) in override

        def via_legal(cx, cy):
            # a through via punches every layer, so every grid must be free
            return all(g[cy * _gnx + cx] == 0 for g in grids)

        openh = [(0, 0, s, None)]
        came = {}
        gc2 = {s: 0}
        pops = 0
        DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
        goal = None
        while openh:
            pops += 1
            if pops > P["astar_multilayer_max_pops"]:
                break
            f, cost, cur, pdir = heapq.heappop(openh)
            if cur == tt:
                goal = cur
                break
            if cost > gc2.get(cur, 1 << 60):
                continue
            cx, cy, li = cur
            for d in DIRS:
                nx2, ny2 = cx + d[0], cy + d[1]
                if not (0 <= nx2 < _gnx and 0 <= ny2 < _gny):
                    continue
                if not free(nx2, ny2, li):
                    continue
                nxt = (nx2, ny2, li)
                ncst = cost + 1 + (2 if pdir and d != pdir else 0)
                if ncst < gc2.get(nxt, 1 << 60):
                    gc2[nxt] = ncst
                    came[nxt] = (cur, d)
                    heapq.heappush(openh, (ncst + abs(nx2 - tt[0]) + abs(ny2 - tt[1]),
                                           ncst, nxt, d))
            if via_legal(cx, cy):
                for lj in range(len(grids)):
                    if lj == li:
                        continue
                    nxt = (cx, cy, lj)
                    ncst = cost + 14
                    if ncst < gc2.get(nxt, 1 << 60):
                        gc2[nxt] = ncst
                        came[nxt] = (cur, None)
                        heapq.heappush(openh, (ncst + abs(cx - tt[0]) + abs(cy - tt[1]),
                                               ncst, nxt, None))
        if goal is None:
            return False
        # reconstruct
        path = [goal]
        cur = goal
        while cur != s:
            cur = came[cur][0]
            path.append(cur)
        path.reverse()

        # convert to runs per layer + via points
        def to_abs(cx, cy):
            return (int(_gox + (cx + 0.5) * GRIDA), int(_goy + (cy + 0.5) * GRIDA))

        runs = []          # (layer_index, [cells])
        cur_run = [path[0]]
        for st in path[1:]:
            if st[2] != cur_run[-1][2]:
                runs.append((cur_run[-1][2], cur_run))
                cur_run = [st]
            else:
                cur_run.append(st)
        runs.append((cur_run[-1][2], cur_run))
        via_pts = []
        for i in range(len(runs) - 1):
            lst = runs[i][1][-1]
            via_pts.append(to_abs(lst[0], lst[1]))
        for vi, vp in enumerate(via_pts):
            if via_ok(vp[0], vp[1], nc):
                continue
            # grid-quantized spot fails precise validation: nudge in a small
            # spiral before giving up (the corridor cell is free, the exact
            # centre may graze something the grid rounded away)
            fixed = None
            for rr in (0.13, 0.25, 0.38, 0.5):
                for q in range(8):
                    nxp = (int(vp[0] + FM(rr) * math.cos(math.pi * q / 4)),
                           int(vp[1] + FM(rr) * math.sin(math.pi * q / 4)))
                    if via_ok(nxp[0], nxp[1], nc):
                        fixed = nxp
                        break
                if fixed:
                    break
            if fixed is None:
                log(f"{net.GetNetname()} {names}: multi-layer via failed "
                    f"re-validation — skipped")
                return False
            via_pts[vi] = fixed
        # emit
        total = 0.0
        for li, run_cells in runs:
            pts = [run_cells[0]]
            for i in range(1, len(run_cells) - 1):
                a, c2, d2 = run_cells[i - 1], run_cells[i], run_cells[i + 1]
                if (c2[0] - a[0], c2[1] - a[1]) != (d2[0] - c2[0], d2[1] - c2[1]):
                    pts.append(c2)
            pts.append(run_cells[-1])
            apts = [to_abs(p[0], p[1]) for p in pts]
            if li == 0 and run_cells is runs[0][1]:
                apts[0] = (int(sx), int(sy))
            if li == 0 and run_cells is runs[-1][1]:
                apts[-1] = (int(tx), int(ty))
            for q in range(len(apts) - 1):
                if apts[q] == apts[q + 1]:
                    continue
                add_track(*apts[q], *apts[q + 1], net, LAYS[li], width)
                total += math.hypot(apts[q + 1][0] - apts[q][0],
                                    apts[q + 1][1] - apts[q][1]) / 1e6
        for vp in via_pts:
            add_via(vp[0], vp[1], net)
        log(f"{net.GetNetname()} {names}: multi-layer A* {total:.1f}mm "
            f"({len(via_pts)} vias)")
        stats["astar_multilayer_links"] += 1
        return True

    # ---- 4c. signal nets: any net with >1 physical cluster gets a link ------
    # Hard-first ordering comes from [route] net_priority: where two nets of a
    # pair share one narrow corridor the first router through claims it, so
    # the one with the harder escape has to pick first and the other
    # parallels it.
    def _prio(i):
        n = b.FindNet(i)
        nm = n.GetNetname() if n else ""
        return (net_priority.index(nm) if nm in net_priority else len(net_priority), i)

    for net_i in sorted(range(b.GetNetCount()), key=_prio):
        net = b.FindNet(net_i)
        if net is None:
            continue
        name = net.GetNetname()
        if not name or name in pour_names or name in fixed_nets \
                or name.startswith("unconnected"):
            continue
        nc = net.GetNetCode()
        n_items = sum(1 for s in segs_all if s[5] == nc) + \
            sum(1 for v in vias_all if v[3] == nc) + \
            sum(1 for p in pads_all if p[4] == nc)
        if n_items < 2:
            continue
        cl = clusters_for(nc)
        if len(cl) < 2:
            continue
        keys = sorted(cl, key=lambda k: -(len(cl[k]["segs"]) + len(cl[k]["vias"]) + len(cl[k]["pads"])))
        main_pts = cluster_points(cl[keys[0]], front) + [(v[0], v[1]) for v in cl[keys[0]]["vias"]]
        for k in keys[1:]:
            c = cl[k]
            names = [p[6] for p in c["pads"]][:3]
            done = False
            scored = sorted((math.hypot(cx - mx, cy - my), (cx, cy), (mx, my))
                            for cx, cy in cluster_points(c, front) for mx, my in main_pts)
            for dist, (cx, cy), (mx, my) in scored[:FRONT_LINK_PAIRS]:
                midx, midy = (cx + mx) // 2, (cy + my) // 2
                for path in ([(cx, cy), (mx, my)],
                             [(cx, cy), (mx, cy), (mx, my)],
                             [(cx, cy), (cx, my), (mx, my)],
                             [(cx, cy), (midx, cy), (midx, my), (mx, my)]):
                    if path_ok(path, nc, front, net_w(net)):
                        for q in range(len(path) - 1):
                            add_track(*path[q], *path[q + 1], net, front, net_w(net))
                        log(f"{name} {names}: {name_of[front]} link {TM(int(dist)):.2f}mm")
                        stats["front_links"] += 1
                        done = True
                        break
                if done:
                    break
            if not done:
                done = layer_link(c, main_pts, net, nc, names)
            if not done:
                done = astar_link(c, main_pts, net, nc, names)
            if not done:
                done = astar_multilayer_link(c, main_pts, net, nc, names)
            if not done:
                log(f"{name} {names}: UNFIXED — manual routing needed")
                unfixed.append(name)

    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(pcb_path), b)
    log("refilled + saved")

    emit(
        "post_route_fix",
        board=str(pcb_path),
        deleted=len(seen_ids),
        clearance_rings=len(rings),
        rule_boxes=len(rule_boxes),
        island_removal_zones=island_zones,
        unfixed=sorted(set(unfixed)),
        segments_before=segs_before,
        segments_after=count_segments(pcb_path),
        **stats,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
