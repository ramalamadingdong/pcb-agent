#!/usr/bin/env python3
"""Post-route dead-copper cleanup.

Runs AFTER post_route_fix.py / finish_routes.py, which only ADD copper.
Three classes of dead copper survive a route otherwise:

  * dangling vias on signal nets — fanout dogbone escapes the router never
    used.  Each punches an antipad through every plane for nothing.
  * dangling track stubs — tails ending in mid-air.
  * exactly-duplicated segments — the same geometry drawn twice by a
    pre-route pass emitting overlapping polylines.

This pass:
  1. removes exact-duplicate segments (same endpoints/layer/width/net) and
     co-located duplicate vias
  2. retags any via matching `[route.via_retag] from` to `to`.  The stale
     size is not a one-off migration to clean up once: the SES round-trip
     RE-CREATES those vias every route, so this retag is permanent pipeline.
     (The usual case is a drill that leaves an annular ring under the fab's
     floor — 0.6/0.4 is a 0.10 mm ring, under JLC's 0.13.)
  3. iteratively prunes dead copper on NON-POUR nets only (pour-net vias are
     plane stitching and are never touched):
       - a via is dead unless it joins >= 2 layers of same-net copper
         (touching tracks per layer; through-hole pads count on all layers,
         SMD pads on their own layer)
       - a segment is dead if either endpoint touches nothing (no pad, no
         via, no other same-net track at that end)
     repeated to fixpoint so chains collapse from the tail inward.
  4. removes everything condemned in one pass, then refills zones + saves.

ONE READ, ONE REMOVAL PASS — and it is not a style preference
------------------------------------------------------------
`b.Remove()` detaches an item from the board's track deque and hands its
ownership to Python.  A `b.GetTracks()` AFTER that comes back, eventually, as a
bare SwigPyObject with no `__iter__`, and the process then dies with SIGSEGV
during interpreter teardown.  It survives the first call or two and fails on a
later one, so it reads as flaky rather than as the container corruption it is.

This pass used to remove duplicates immediately and then re-enumerate the board
once per fixpoint round.  On a 1300-track board that was 240 removals followed
by a third `GetTracks()`, which crashed — and because the crash landed before
`SaveBoard`, the pass was all-or-nothing: the dedupe, the retag and the whole
prune were discarded, and `route.py` aborted at stage 6/7 before DRC ever ran.

So the board is read exactly once, up front; every stage decides from plain
tuples keyed by `id()` of the snapshot's proxies; and the single mutation runs
at the very end, after which nothing reads the board through pcbnew again.  The
fixpoint is equivalent to re-reading a board with the condemned items gone,
because a survivor's geometry does not change when a neighbour is deleted.

`add_keepouts.py` ("segfaults hard") and `zones.py` carry the same warning for
the footprint and zone containers.  `finish_routes.py` and `post_route_fix.py`
still re-enumerate after their own removals; they get away with it because they
remove far fewer items.

Never prunes pour nets: a pour-net via legitimately "dangles" into a plane.
Verify with DRC afterwards (sampled — DRC is nondeterministic).

Idempotent: everything here is a removal or a retag derived from the board's
own geometry, so a second run finds nothing left to do.

board.toml keys consumed:
  [layers] front, back              outer copper layers (default F_Cu, B_Cu)
  [[zones.pour]] net                pour nets, unioned with every net that
  [[route.pours]] net               owns a filled zone (the zone scan is the
                                    primary source and needs no config)
  [route.via_retag] from = { size_mm, drill_mm }
  [route.via_retag] to   = { size_mm, drill_mm }
  [route.via_retag] tolerance_mm    match window (default 0.01)

Not ported: the refdes/net names and the castellation x-coordinates the
source quoted as the observed instances of each defect class.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import count_segments, emit, fail, load_config, pass_parser  # noqa: E402

EPS = 1000  # 1 um

RETAG_DEFAULT = {
    "from": {"size_mm": 0.6, "drill_mm": 0.4},
    "to": {"size_mm": 0.6, "drill_mm": 0.3},
    "tolerance_mm": 0.01,
}


def log(*a) -> None:
    print(*a, file=sys.stderr)


def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses (``In1_Cu``) as well as
    KiCad's own (``In1.Cu``).  board.toml is read by both."""
    return name.replace("_Cu", ".Cu").replace("_", ".")


def d_pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def in_rect(px, py, cx, cy, hw, hh, margin):
    return abs(px - cx) <= hw + margin and abs(py - cy) <= hh + margin


def main() -> int:
    ap = pass_parser("cleanup_pass")
    ap.add_argument("--dedupe-only", action="store_true",
                    help="pre-route safe mode: skip the dead-copper prune "
                         "(pre-route, every fanout via is legitimately dangling)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    route_cfg = cfg.get("route", {})

    retag_cfg = dict(RETAG_DEFAULT)
    retag_cfg.update(route_cfg.get("via_retag") or {})
    r_from = {**RETAG_DEFAULT["from"], **(retag_cfg.get("from") or {})}
    r_to = {**RETAG_DEFAULT["to"], **(retag_cfg.get("to") or {})}
    r_tol = float(retag_cfg.get("tolerance_mm", RETAG_DEFAULT["tolerance_mm"]))

    pcb_path = Path(args.board)
    segs_before = count_segments(pcb_path)
    b = pcbnew.LoadBoard(str(pcb_path))
    if b is None:
        fail(f"pcbnew could not load {pcb_path} (KiCad major mismatch?)")
    FM, TM = pcbnew.FromMM, pcbnew.ToMM

    lay_cfg = cfg.get("layers", {})
    front_name = lay_cfg.get("front", "F_Cu")
    back_name = lay_cfg.get("back", "B_Cu")
    FCU, BCU = (b.GetLayerID(layer_name(front_name)),
                b.GetLayerID(layer_name(back_name)))
    if FCU < 0 or BCU < 0:
        fail(f"[layers] front/back ({front_name}/{back_name}) are not on this board")

    def via_width(v):
        # KiCad 10: PCB_VIA.GetWidth() without a layer argument asserts and
        # returns garbage (padstacks made the width per-layer).
        try:
            return v.GetWidth(FCU)
        except TypeError:
            return v.GetWidth()

    # Pour nets: every net that owns a filled zone, plus any declared in
    # [[zones.pour]] / [[route.pours]] whose zone has not been created yet.
    pour_netcodes = set()
    for z in b.Zones():
        if not z.GetIsRuleArea():
            pour_netcodes.add(z.GetNetCode())
    declared = [p.get("net") for p in (route_cfg.get("pours") or [])]
    declared += [z.get("net") for z in (cfg.get("zones", {}).get("pour") or [])]
    for nm in declared:
        net = b.FindNet(nm) if nm else None
        if net is not None:
            pour_netcodes.add(net.GetNetCode())

    # ---- 0. ONE snapshot of every track, taken before any mutation ----------
    # b.Remove() detaches an item from the board's std::deque AND flips its
    # ownership to Python. That leaves the wrapper behind Tracks() stale: a
    # LATER b.GetTracks() hands back a bare SwigPyObject with no __iter__, and
    # the interpreter then segfaults at teardown walking the damaged
    # container. It survives one or two calls and dies on a later one, which
    # is why this read as intermittent.
    #
    # So: read the board exactly once, decide everything from plain tuples,
    # and defer every removal to a single pass at the end. No pcbnew accessor
    # is called after the first Remove(). Same hazard as add_keepouts.py:141
    # ("segfaults hard") and zones.py:104, one container along.
    #
    # `tracks` also holds the only strong references to these proxies, which
    # is what makes id() a stable key below -- a fresh GetTracks() would mint
    # new proxy objects for the same C++ items and invalidate every id.
    tracks = []     # (id, kind, obj), in board order
    via_snap = {}   # id -> [x, y, width, netcode, obj]  (width rewritten by retag)
    seg_snap = {}   # id -> (sx, sy, ex, ey, width, layer, netcode, obj)
    for t in b.GetTracks():
        tid = id(t)
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            via_snap[tid] = [int(p.x), int(p.y), via_width(t), t.GetNetCode(), t]
            tracks.append((tid, "via", t))
        else:
            s, e = t.GetStart(), t.GetEnd()
            seg_snap[tid] = (int(s.x), int(s.y), int(e.x), int(e.y),
                             t.GetWidth(), t.GetLayer(), t.GetNetCode(), t)
            tracks.append((tid, "seg", t))
    log(f"snapshot: {len(seg_snap)} segment(s), {len(via_snap)} via(s)")

    # Every id marked here is removed exactly once, by the single pass that
    # runs just before the refill at the bottom of main().
    doomed = set()

    # ---- 1. exact duplicates (segments AND co-located vias) -----------------
    seen, ndup_v, ndup_s = set(), 0, 0
    for tid, kind, _t in tracks:
        if kind == "via":
            x, y, _w, nc, _o = via_snap[tid]
            key = ("via", x, y, nc)
            if key in seen:
                doomed.add(tid)
                ndup_v += 1
            else:
                seen.add(key)
            continue
        sx, sy, ex, ey, w, lay, nc, _o = seg_snap[tid]
        key = (min((sx, sy), (ex, ey)), max((sx, sy), (ex, ey)), lay, w, nc)
        if key in seen:
            doomed.add(tid)
            ndup_s += 1
        else:
            seen.add(key)
    log(f"duplicates marked: {ndup_s} segment(s), {ndup_v} via(s)")

    # ---- 2. stale via retag -------------------------------------------------
    # Permanent pipeline, not a migration: the SES round-trip re-creates these
    # vias at the stale size on every route, so the retag has to run after
    # every import for as long as the router is in the chain.
    # Setters only -- SetWidth/SetDrill mutate the item, never the board's
    # track container, so they are safe here. The snapshot's width is updated
    # in step with them so the prune below measures the retagged geometry
    # rather than the size the SES import left behind.
    retag = 0
    for tid, kind, t in tracks:
        if kind != "via" or tid in doomed:
            continue
        if abs(TM(via_snap[tid][2]) - float(r_from["size_mm"])) < r_tol and \
                abs(TM(t.GetDrillValue()) - float(r_from["drill_mm"])) < r_tol:
            new_w = FM(float(r_to["size_mm"]))
            t.SetWidth(new_w)
            t.SetDrill(FM(float(r_to["drill_mm"])))
            via_snap[tid][2] = new_w
            retag += 1
    log(f"vias retagged {r_from['size_mm']}/{r_from['drill_mm']} -> "
        f"{r_to['size_mm']}/{r_to['drill_mm']}: {retag}")

    # BOARD.Remove()'s own docstring: "set the thisdown flag so that the python
    # wrapper owns the C++ BOARD_ITEM". That ownership flip is the whole
    # problem -- it is what leaves the container's wrapper stale and what makes
    # the interpreter delete 260 detached tracks at teardown, printing a
    # "swig/python detected a memory leak of type 'PCB_TRACK *'" line for each.
    # RemoveNative() detaches without transferring ownership: the proxies in
    # `tracks` stay valid pointers, nothing is deleted under us, and the log
    # stays readable. Fall back to Remove() on a KiCad that lacks it.
    remove = getattr(b, "RemoveNative", None) or b.Remove

    def apply_removals():
        """The one and only mutation of the board's track container.

        Runs once, after every decision is made, and nothing reads the board
        through pcbnew afterwards. `tracks` holds exactly one entry per item,
        so nothing can be handed to remove() twice -- a double detach is a
        double-free, which is what the id()-keyed bookkeeping above prevents.
        """
        n = 0
        for tid, _kind, t in tracks:
            if tid in doomed:
                remove(t)
                n += 1
        log(f"removed {n} item(s) in one pass")
        return n

    # ---- 3. dead-copper prune (non-pour nets) -------------------------------
    if args.dedupe_only:
        apply_removals()
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        pcbnew.SaveBoard(str(pcb_path), b)
        log("dedupe-only: prune skipped, refilled + saved")
        emit(
            "cleanup_pass",
            board=str(pcb_path),
            mode="dedupe-only",
            duplicate_segments=ndup_s,
            duplicate_vias=ndup_v,
            vias_retagged=retag,
            pruned_vias=0,
            pruned_segments=0,
            segments_before=segs_before,
            segments_after=count_segments(pcb_path),
        )
        return 0

    pads = []   # (x, y, hw, hh, netcode, is_tht, layer)
    for fp in b.GetFootprints():
        for p in fp.Pads():
            pos = p.GetPosition()
            sz = p.GetSize()
            ang = abs(p.GetOrientationDegrees()) % 180
            hw, hh = (sz.y / 2, sz.x / 2) if abs(ang - 90) < 1 else (sz.x / 2, sz.y / 2)
            lay = FCU if p.IsOnLayer(FCU) else BCU
            pads.append((pos.x, pos.y, hw, hh, p.GetNetCode(), p.HasHole(), lay))
    pads_by_net = defaultdict(list)
    for p in pads:
        pads_by_net[p[4]].append(p)

    # The fixpoint runs entirely in Python, over the step-0 snapshot. Each
    # round re-derives the live geometry by filtering out what earlier rounds
    # condemned, which is exactly equivalent to re-reading a board that had
    # those items removed -- the survivors' geometry cannot change -- and it
    # never touches the board again. Re-enumerating b.GetTracks() here is what
    # crashed: by the third call the container wrapper was stale.
    pruned_v, pruned_s = 0, 0
    while True:
        seg_data = [(sx, sy, ex, ey, w // 2, lay, nc, o)
                    for tid, (sx, sy, ex, ey, w, lay, nc, o) in seg_snap.items()
                    if tid not in doomed]
        via_data = [(x, y, w // 2, nc, o)
                    for tid, (x, y, w, nc, o) in via_snap.items()
                    if tid not in doomed]
        segs_by_net = defaultdict(list)
        for sd in seg_data:
            segs_by_net[sd[6]].append(sd)
        vias_by_net = defaultdict(list)
        for vd in via_data:
            vias_by_net[vd[3]].append(vd)

        kill_ids = set()   # id()-based: SWIG proxy __eq__ is not identity
        # dead vias: must join >= 2 distinct layers of same-net copper
        for vx, vy, vr, nc, obj in via_data:
            if nc in pour_netcodes:
                continue
            layers = set()
            for sx, sy, ex, ey, hw, lay, _nc, _o in segs_by_net[nc]:
                if d_pt_seg(vx, vy, sx, sy, ex, ey) <= vr + hw + EPS:
                    layers.add(lay)
            for px, py, hw, hh, pnc, tht, play in pads_by_net[nc]:
                if in_rect(vx, vy, px, py, hw, hh, vr):
                    if tht:
                        layers.update((FCU, BCU))
                    else:
                        layers.add(play)
            if len(layers) < 2:
                kill_ids.add(id(obj))
        # dead segments: an endpoint touching no same-net copper
        for sx, sy, ex, ey, hw, lay, nc, obj in seg_data:
            if nc in pour_netcodes:
                continue
            for ptx, pty in ((sx, sy), (ex, ey)):
                ok = False
                for px, py, phw, phh, pnc, tht, play in pads_by_net[nc]:
                    if (tht or play == lay) and in_rect(ptx, pty, px, py, phw, phh, hw):
                        ok = True
                        break
                if not ok:
                    for vx, vy, vr, _nc, vobj in vias_by_net[nc]:
                        if id(vobj) in kill_ids:
                            continue
                        if math.hypot(ptx - vx, pty - vy) <= vr + hw + EPS:
                            ok = True
                            break
                if not ok:
                    for osx, osy, oex, oey, ohw, olay, _nc, oobj in segs_by_net[nc]:
                        if oobj is obj or olay != lay:
                            continue
                        if d_pt_seg(ptx, pty, osx, osy, oex, oey) <= ohw + hw + EPS:
                            ok = True
                            break
                if not ok:
                    kill_ids.add(id(obj))
                    break
        if not kill_ids:
            break
        # Condemn, don't remove: the board is left alone until apply_removals().
        # Classify from the snapshot dicts rather than obj.GetClass(), which is
        # one more accessor call this loop no longer needs to make.
        for tid in kill_ids:
            if tid in via_snap:
                pruned_v += 1
            else:
                pruned_s += 1
        doomed |= kill_ids
    log(f"dead copper pruned: {pruned_v} via(s), {pruned_s} segment(s)")

    apply_removals()
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(pcb_path), b)
    log("refilled + saved")

    emit(
        "cleanup_pass",
        board=str(pcb_path),
        mode="full",
        duplicate_segments=ndup_s,
        duplicate_vias=ndup_v,
        vias_retagged=retag,
        pruned_vias=pruned_v,
        pruned_segments=pruned_s,
        segments_before=segs_before,
        segments_after=count_segments(pcb_path),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
