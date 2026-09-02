#!/usr/bin/env python3
"""Silkscreen finishing pass.

Two jobs:

1. REFERENCE REPAIR — collision-aware.  The placement optimiser writes
   ABSOLUTE board coordinates into each footprint's footprint-LOCAL
   Reference/Value property ``(at ...)`` — the same corruption
   ``repair_pads.py`` fixes for pads, but nothing ever repaired the text
   fields.  Result on the board this was ported from: ~60 refdes rendered
   150-300 mm off-board and the fab clipped them, so those boards printed
   with almost no designators.  **This is why the pass repositions EVERY
   refdes on the board rather than only the ones that look wrong** — a
   corrupted local coordinate is indistinguishable from an intentional one.

   The naive repair (fixed spot 1.1 mm above the courtyard) produced 54
   silk_over_copper + 43 silk_overlap DRC warnings on a dense board — the
   fab clips silk at the mask opening, so a refdes over a pad prints with
   missing letters.  This pass instead SEARCHES for a clear spot per
   Reference: above / below / left / right of the courtyard at 1.1 mm, then
   the four diagonals, then wider offsets (1.6, 2.1 mm), then the whole
   ladder again with the text shrunk 1.0 -> 0.85 mm.  A candidate is
   accepted only if its text bbox
     - stays edge_margin_mm inside the board outline,
     - stays out of every declared [[keepouts]] rectangle,
     - clears every pad's solder-mask opening (pad bbox + 0.1 mm; assumes
       pad_to_mask_clearance 0, i.e. opening == pad),
     - clears every previously-placed Reference, every SILK_MARKINGS item,
       and every footprint's own silk graphics.
   If the whole prescribed ladder is blocked (an LED label row or a dense
   connector cluster will do it), a last-resort SLIDE extension tries wider
   gaps (to 4.1 mm) and positions shifted along the edge (±0.9..5.4 mm) so a
   refdes can stagger past a label row; only if even that fails is the
   least-bad candidate (fewest collisions, pads weighted worst) kept and a
   WARNING printed.  Text is never rotated (left/right spots stay
   horizontal).  Value goes just below the part on the fab layer (fab has no
   clipping problem — no search needed).

2. MARKINGS.  Whatever text and lines board.toml declares — a forward-axis
   arrow, a battery polarity mark, the revision string, button and LED
   labels.  Everything lands in a PCB group named SILK_MARKINGS, which is
   stripped on re-run — idempotent.  The markings are placed FIRST (fixed
   positions), so the Reference search above can dodge them.

   Markings are never MOVED: their coordinates are the author's, and where a
   label sits is the whole point of it.  That once made them the only silk on
   the board nothing checked — every refdes was dodging pads while a declared
   marking could sit straight on top of one.  So each declared marking is now
   tested against the pad mask openings and a collision is WARNED about, by
   the board.toml line that declared it.  It stays a warning, not a failure:
   the pass cannot know a better position, only that this one prints badly.

Run after placement is final.  Safe to re-run at any later point, including
after routing — the search is deterministic, so re-running reproduces the
same placements.  (Geometry is reproduced exactly; the KIIDs pcbnew mints
for new items are not settable from Python, so the file is not byte-identical
at the UUID level.)

board.toml
----------
::

    [silk]
    edge_margin_mm     = 0.3     # default: [silkscreen].margin_mm, else 0.3
    keepout_margin_mm  = 0.3     # extra margin around [[keepouts]] rects
    pad_mask_margin_mm = 0.1
    text_tiers         = [[1.0, 0.15], [0.85, 0.15]]   # [size mm, thickness mm]
    gaps_mm            = [1.1, 1.6, 2.1]
    char_width_factor  = 0.95

    [[silk.text]]
    text  = "rev A"
    x     = 16.0        # absolute board mm
    y     = 88.6
    size  = 1.0         # optional
    layer = "F.SilkS"   # optional
    bold  = false       # optional

    [[silk.line]]
    x1 = 28.0
    y1 = 57.5
    x2 = 28.0
    y2 = 55.0
    width = 0.25        # optional
    layer = "F.SilkS"   # optional

Not ported
----------
The source pass hard-coded its board's drawn markings — an IMU forward-axis
arrow with its two arrowhead strokes, ``+``/``-`` battery polarity marks read
from the live pad positions of a named connector, a revision string, BOOT/RST
button labels and five LED function labels — plus a module antenna keepout
band it had to dodge.  All of those are one board's markings, so they are
gone: declare yours as ``[[silk.text]]`` / ``[[silk.line]]`` entries, and
declare the antenna band as a ``[[keepouts]]`` rectangle (which it is, and
which the gerber checker then verifies too).  Polarity marks placed from a
connector's live pad net names have no generic form and were dropped
outright — write the two coordinates.

Usage:
    python3 silk_finish.py --board board.kicad_pcb --config board.toml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pcbnew

import _lib

PASS = "silk_finish"

GROUP_NAME = "SILK_MARKINGS"

EDGE_MARGIN_MM = 0.3       # refdes bbox must stay this far inside the outline
KEEPOUT_MARGIN_MM = 0.3    # extra margin around a declared keepout rectangle
PAD_MASK_MARGIN_MM = 0.1   # mask opening == pad bbox (clearance 0) + this

# Refdes candidate ladder: full-size text first, then shrunk-text retry.
# Typical fab silk minimums: 0.8 mm text height, 0.15 mm line width — the
# shrink tier must NOT go below 0.15 thickness (0.13 prints unreliably).
REF_TEXT_TIERS = ((1.0, 0.15), (0.85, 0.15))   # (size mm, thickness mm)
REF_GAPS_MM = (1.1, 1.6, 2.1)                  # courtyard-edge offsets
CHAR_W_FACTOR = 0.95       # stroke-font per-character width estimate
CAND_LABELS = ("above", "below", "left", "right", "nw", "ne", "sw", "se")

# Last-resort slide extension (only reached when the prescribed ladder is
# fully blocked): wider gaps + positions shifted along the courtyard edge.
EXT_GAPS_MM = (1.1, 1.6, 2.1, 2.6, 3.1, 3.6, 4.1)
EXT_XSHIFTS_MM = (0.0, -0.9, 0.9, -1.8, 1.8, -2.7, 2.7, -3.6, 3.6,
                  -4.5, 4.5, -5.4, 5.4)
EXT_YSHIFTS_MM = (0.0, -0.9, 0.9, -1.8, 1.8, -2.7, 2.7, -3.6, 3.6)

_LAYER_ALIASES = {
    "F_SILKSCREEN": "F.SilkS",
    "B_SILKSCREEN": "B.SilkS",
    "F_SILKS": "F.SilkS",
    "B_SILKS": "B.SilkS",
    "F_FAB": "F.Fab",
    "B_FAB": "B.Fab",
}


def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses as well as KiCad's own."""
    key = name.upper().replace(".", "_")
    return _LAYER_ALIASES.get(key, name.replace("_Cu", ".Cu"))


def mm(v):
    return pcbnew.FromMM(v)


def rect_of(box):
    """BOX2I -> (l, t, r, b) plain-int tuple (safe across SWIG mutations)."""
    return (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())


def rects_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def natkey(ref):
    m = re.match(r"([A-Za-z_]+)(\d+)$", ref)
    if m:
        return (m.group(1), int(m.group(2)))
    return (ref, 0)


def main() -> int:
    args = _lib.pass_parser(PASS).parse_args()
    pcb_path = Path(args.board)
    if not pcb_path.exists():
        _lib.fail(f"{pcb_path}: no such board")

    cfg = _lib.load_config(args.config)
    scfg = cfg.get("silk", {}) or {}

    edge_margin = float(
        scfg.get(
            "edge_margin_mm",
            cfg.get("silkscreen", {}).get("margin_mm", EDGE_MARGIN_MM),
        )
    )
    keepout_margin = float(scfg.get("keepout_margin_mm", KEEPOUT_MARGIN_MM))
    pad_mask_margin = float(scfg.get("pad_mask_margin_mm", PAD_MASK_MARGIN_MM))
    char_w_factor = float(scfg.get("char_width_factor", CHAR_W_FACTOR))
    tiers_cfg = scfg.get("text_tiers") or REF_TEXT_TIERS
    ref_tiers = tuple((float(t[0]), float(t[1])) for t in tiers_cfg)
    ref_gaps = tuple(float(g) for g in (scfg.get("gaps_mm") or REF_GAPS_MM))

    b = pcbnew.LoadBoard(str(pcb_path))
    f_silk = b.GetLayerID("F.SilkS")
    b_silk = b.GetLayerID("B.SilkS")
    f_fab = b.GetLayerID("F.Fab")
    b_fab = b.GetLayerID("B.Fab")
    f_crtyd = b.GetLayerID("F.CrtYd")
    b_crtyd = b.GetLayerID("B.CrtYd")
    silk_layers = {f_silk, b_silk}

    bb = b.GetBoardEdgesBoundingBox()
    if bb.GetWidth() <= 0 or bb.GetHeight() <= 0:
        _lib.fail("no Edge.Cuts outline found -- cannot place silkscreen")
    ox, oy = bb.GetLeft(), bb.GetTop()
    frame = _lib.board_frame(b)

    def abs_pt(x, y):
        """board.toml coordinates are board-frame (bottom-left, Y-UP) — the
        same frame [[keepouts]] and [frame] use — converted to KiCad page
        coordinates here (see _lib.board_frame)."""
        kx, ky = _lib.to_kicad_xy(frame, x, y)
        return pcbnew.VECTOR2I(mm(kx), mm(ky))

    # ------------------------------------------------------------------ 1 ----
    # MARKINGS first: their positions are FIXED, and the refdes search below
    # must dodge them, so they have to exist before any Reference moves.
    # Strip a previous SILK_MARKINGS group (idempotency): collect first —
    # pcbnew SWIG wrappers go stale after container changes.
    doomed_groups = [g for g in b.Groups() if g.GetName() == GROUP_NAME]
    doomed_items = []
    for g in doomed_groups:
        doomed_items.extend(list(g.GetItems()))
    for it in doomed_items:
        b.Remove(it)
    for g in doomed_groups:
        b.Remove(g)
    if doomed_groups:
        print(
            f"  stripped previous {GROUP_NAME} ({len(doomed_items)} items)",
            file=sys.stderr,
        )

    # Obstacles the refdes search must clear: (rect, kind, layer) with kind
    # used for scoring ('pad' collisions are the worst — the fab clips silk at
    # the mask opening).  layer None means "applies to both silk sides".
    obstacles = []

    # Any board-level silk that is NOT ours (collected after the strip, so a
    # previous run's markings don't count twice).
    for d in list(b.GetDrawings()):
        if d.GetLayer() in silk_layers:
            obstacles.append((rect_of(d.GetBoundingBox()), "silk", d.GetLayer()))

    group = pcbnew.PCB_GROUP(b)
    group.SetName(GROUP_NAME)
    b.Add(group)
    added = []
    # (item, "[[silk.text]] #n", label) for every marking the config declared,
    # so a collision can be reported against the line the author wrote.
    declared = []

    def text(s, x, y, size=1.0, thickness=None, bold=False, layer=None):
        t = pcbnew.PCB_TEXT(b)
        t.SetText(s)
        t.SetPosition(abs_pt(x, y))
        t.SetLayer(f_silk if layer is None else layer)
        t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        t.SetTextThickness(mm(thickness if thickness else (0.2 if bold else 0.15)))
        b.Add(t)
        added.append(t)
        return t

    def line(x0, y0, x1, y1, w=0.2, layer=None):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(abs_pt(x0, y0))
        s.SetEnd(abs_pt(x1, y1))
        s.SetLayer(f_silk if layer is None else layer)
        s.SetWidth(mm(w))
        b.Add(s)
        added.append(s)
        return s

    def resolve_layer(raw, default, where):
        if raw is None:
            return default
        n = layer_name(str(raw))
        lid = b.GetLayerID(n)
        if lid < 0:
            _lib.fail(f"{where}: layer '{raw}' is not on this board")
        return lid

    for i, item in enumerate(scfg.get("text", []) or []):
        where = f"[[silk.text]] #{i + 1}"
        missing = [k for k in ("text", "x", "y") if k not in item]
        if missing:
            _lib.fail(f"{where} is missing " + ", ".join(missing))
        declared.append((
            text(
                str(item["text"]),
                float(item["x"]),
                float(item["y"]),
                size=float(item.get("size", 1.0)),
                thickness=item.get("thickness_mm"),
                bold=bool(item.get("bold", False)),
                layer=resolve_layer(item.get("layer"), f_silk, where),
            ),
            where,
            str(item["text"]),
        ))

    for i, item in enumerate(scfg.get("line", []) or []):
        where = f"[[silk.line]] #{i + 1}"
        missing = [k for k in ("x1", "y1", "x2", "y2") if k not in item]
        if missing:
            _lib.fail(f"{where} is missing " + ", ".join(missing))
        declared.append((
            line(
                float(item["x1"]),
                float(item["y1"]),
                float(item["x2"]),
                float(item["y2"]),
                w=float(item.get("width", 0.2)),
                layer=resolve_layer(item.get("layer"), f_silk, where),
            ),
            where,
            "line",
        ))

    for it in added:
        group.AddItem(it)
        obstacles.append((rect_of(it.GetBoundingBox()), "marking", it.GetLayer()))
    print(f"  {GROUP_NAME}: {len(added)} silk items added", file=sys.stderr)

    # ------------------------------------------------------------------ 2 ----
    # Remaining obstacles: every pad's mask opening and every footprint's own
    # silk graphics.  Collected into plain tuples before any further use.
    footprints = sorted(list(b.GetFootprints()),
                        key=lambda f: natkey(f.GetReference()))
    named_pads = []
    for fp in footprints:
        for pad in fp.Pads():
            pb = pad.GetBoundingBox()
            pb.Inflate(mm(pad_mask_margin))
            # A pad blocks silk on whichever side it appears; a through-hole
            # pad appears on both, so pads are registered layer-agnostic.
            obstacles.append((rect_of(pb), "pad", None))
            named_pads.append(
                (rect_of(pb), f"{fp.GetReference()}.{pad.GetNumber()}")
            )
        for gi in fp.GraphicalItems():
            if gi.GetLayer() in silk_layers:
                obstacles.append(
                    (rect_of(gi.GetBoundingBox()), "silk", gi.GetLayer())
                )

    # ------------------------------------------------------------------ 3 ----
    # Collision-aware Reference placement + Value repair.
    board_l = ox + mm(edge_margin)
    board_t = oy + mm(edge_margin)
    board_r = bb.GetRight() - mm(edge_margin)
    board_b = bb.GetBottom() - mm(edge_margin)

    # Declared keepouts are silk keepouts too: a module's PCB-antenna band
    # wants nothing printed over it either.  Same rectangles the gerber
    # checker reads — board-frame (bottom-left, Y-up), converted here
    # (see _lib.board_frame).
    keepout_rects = []
    for ko in cfg.get("keepouts", []) or []:
        if not all(k in ko for k in ("x1", "y1", "x2", "y2")):
            continue
        kx1, ky1 = _lib.to_kicad_xy(frame, float(ko["x1"]), float(ko["y1"]))
        kx2, ky2 = _lib.to_kicad_xy(frame, float(ko["x2"]), float(ko["y2"]))
        x1, x2 = sorted((kx1, kx2))
        y1, y2 = sorted((ky1, ky2))
        keepout_rects.append(
            (
                mm(x1 - keepout_margin),
                mm(y1 - keepout_margin),
                mm(x2 + keepout_margin),
                mm(y2 + keepout_margin),
            )
        )

    def hard_ok(r):
        """Board-outline + keepout constraints: never violated."""
        if not (r[0] >= board_l and r[1] >= board_t and
                r[2] <= board_r and r[3] <= board_b):
            return False
        for k in keepout_rects:
            if rects_overlap(r, k):
                return False
        return True

    def candidates(box, w, h, gap_mm):
        """8 center positions around a courtyard box, sides then diagonals.

        above/below sit the text center gap_mm off the edge (the historical
        convention); left/right/diagonals reproduce the same visual bbox
        clearance (gap - h/2) so all sides look even.  Text stays horizontal.
        """
        l, t, r, bo = box
        ccx = (l + r) // 2
        ccy = (t + bo) // 2
        g = mm(gap_mm)
        c = max(g - h // 2, mm(0.25))   # bbox clearance for the x direction
        left_x = l - c - w // 2
        right_x = r + c + w // 2
        return (
            (ccx, t - g),          # above
            (ccx, bo + g),         # below
            (left_x, ccy),         # left
            (right_x, ccy),        # right
            (left_x, t - g),       # nw
            (right_x, t - g),      # ne
            (left_x, bo + g),      # sw
            (right_x, bo + g),     # se
        )

    def slide_candidates(box, w, h, gap_mm):
        """Extension spots: above/below shifted along x, left/right along y."""
        l, t, r, bo = box
        ccx = (l + r) // 2
        ccy = (t + bo) // 2
        g = mm(gap_mm)
        c = max(g - h // 2, mm(0.25))
        out = []
        for sh in EXT_XSHIFTS_MM:
            s = mm(sh)
            out.append((ccx + s, t - g, f"above{sh:+g}@{gap_mm}"))
            out.append((ccx + s, bo + g, f"below{sh:+g}@{gap_mm}"))
        for sh in EXT_YSHIFTS_MM:
            s = mm(sh)
            out.append((l - c - w // 2, ccy + s, f"left{sh:+g}@{gap_mm}"))
            out.append((r + c + w // 2, ccy + s, f"right{sh:+g}@{gap_mm}"))
        return out

    warnings = []

    # Declared markings are placed at the author's coordinates and are never
    # moved (their position is meaningful — they label a specific part). That
    # makes them the one silk item nothing else checks: the refdes search
    # below treats them as fixed obstacles and dodges THEM. So test them
    # against the pad mask openings here and say so plainly. A fab clips silk
    # at the mask opening, so a marking over a pad prints broken and lands ink
    # on a solderable surface.
    for it, where, label in declared:
        r = rect_of(it.GetBoundingBox())
        hits = [name for prect, name in named_pads if rects_overlap(r, prect)]
        if hits:
            shown = ", ".join(sorted(set(hits))[:4])
            more = "" if len(set(hits)) <= 4 else f" (+{len(set(hits)) - 4} more)"
            warnings.append(
                f'{where} "{label}" overlaps pad(s) {shown}{more} — the fab '
                f"clips silk at the mask opening, so this prints broken and "
                f"puts ink on a solderable pad. Move it in board.toml."
            )

    fixed = 0
    for fp in footprints:
        flipped = fp.IsFlipped()
        silk = b_silk if flipped else f_silk
        fab = b_fab if flipped else f_fab
        crtyd = b_crtyd if flipped else f_crtyd

        court = fp.GetCourtyard(crtyd)
        if court.OutlineCount() > 0:
            cbox = court.BBox()
        else:
            cbox = fp.GetBoundingBox(False)
        box = rect_of(cbox)
        cx_mid = (box[0] + box[2]) // 2
        cy_mid = (box[1] + box[3]) // 2

        # Value: just below the part on the fab layer (fab layer — no mask
        # clipping, no collision search needed).
        val = fp.Value()
        val.SetPosition(pcbnew.VECTOR2I(cx_mid, box[3] + mm(1.1)))
        val.SetLayer(fab)
        val.SetTextSize(pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
        val.SetTextThickness(mm(0.15))
        val.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))

        ref = fp.Reference()
        if not ref.IsVisible():
            continue
        txt = fp.GetReference()

        ref.SetLayer(silk)
        # text angle is ABSOLUTE in this pcbnew API (verified by render:
        # -fp_rot made rotated footprints' refs vertical) - 0 = horizontal
        ref.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        ref.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        ref.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)

        # Measure each tier's text extents once: the estimated bbox per the
        # stroke-font rule of thumb, widened to the renderer's actual extents
        # if those are bigger (conservative in both directions).
        tiers = []
        for size, th in ref_tiers:
            ref.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
            ref.SetTextThickness(mm(th))
            ref.SetPosition(pcbnew.VECTOR2I(cx_mid, cy_mid))
            abox = ref.GetBoundingBox()
            w = max(int(len(txt) * mm(size) * char_w_factor) + mm(th),
                    abox.GetWidth())
            h = max(mm(size) + mm(th), abox.GetHeight())
            tiers.append((size, th, w, h))

        def all_candidates(tiers=tiers, box=box):
            # Prescribed preference ladder: sides then diagonals per gap,
            # full-size text first, then the whole thing shrunk.
            for size, th, w, h in tiers:
                for gap in ref_gaps:
                    for label, (ccx, ccy) in zip(CAND_LABELS,
                                                 candidates(box, w, h, gap)):
                        yield size, th, w, h, ccx, ccy, f"{label}@{gap}/{size}"
            # Last-resort slide extension (dense rows: label rows, connector
            # clusters).
            for size, th, w, h in tiers:
                for gap in EXT_GAPS_MM:
                    for ccx, ccy, label in slide_candidates(box, w, h, gap):
                        yield size, th, w, h, ccx, ccy, f"{label}/{size}"

        chosen = None      # (cx, cy, size, th, rect, label)
        best = None        # (score_key, same-tuple) least-bad fallback
        ladder_idx = 0
        for size, th, w, h, ccx, ccy, label in all_candidates():
            ladder_idx += 1
            rect = (ccx - w // 2, ccy - h // 2,
                    ccx + w // 2, ccy + h // 2)
            if not hard_ok(rect):
                continue
            pad_hits = 0
            other_hits = 0
            for orect, kind, olayer in obstacles:
                if olayer is not None and olayer != silk:
                    continue
                if rects_overlap(rect, orect):
                    if kind == "pad":
                        pad_hits += 1
                    else:
                        other_hits += 1
            cand = (ccx, ccy, size, th, rect, label)
            if pad_hits == 0 and other_hits == 0:
                chosen = cand
                break
            key = (pad_hits, other_hits, ladder_idx)
            if best is None or key < best[0]:
                best = (key, cand)

        if chosen is None:
            if best is not None:
                key, chosen = best
                warnings.append(
                    f"{txt}: no clear spot — kept {chosen[5]} with "
                    f"{key[0]} pad + {key[1]} silk collisions")
            else:
                # Pathological (no candidate even fits the outline): park it
                # below at the historical spot so it at least stays on-board.
                size, th = ref_tiers[0]
                chosen = (cx_mid, box[3] + mm(1.1), size, th, None, "below!")
                warnings.append(f"{txt}: no candidate inside the outline")

        ccx, ccy, size, th, rect, label = chosen
        ref.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        ref.SetTextThickness(mm(th))
        ref.SetPosition(pcbnew.VECTOR2I(ccx, ccy))
        # Register what was ACTUALLY placed (estimate ∪ rendered bbox) so the
        # remaining references dodge reality, not just the estimate.
        abox = rect_of(ref.GetBoundingBox())
        if rect is None:
            union = abox
        else:
            union = (min(rect[0], abox[0]), min(rect[1], abox[1]),
                     max(rect[2], abox[2]), max(rect[3], abox[3]))
        obstacles.append((union, "ref", silk))
        fixed += 1

    print(
        f"  reference/value fields repositioned on {fixed} footprints "
        f"(collision-aware)",
        file=sys.stderr,
    )
    for w in warnings:
        print(f"  WARNING: {w}", file=sys.stderr)

    pcbnew.SaveBoard(str(pcb_path), b)
    _lib.assert_net_table(pcb_path)
    print("saved", file=sys.stderr)

    _lib.emit(
        PASS,
        board=str(pcb_path),
        markings=len(added),
        markings_stripped=len(doomed_items),
        references_repositioned=fixed,
        keepouts_dodged=len(keepout_rects),
        edge_margin_mm=edge_margin,
        warnings=warnings,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
