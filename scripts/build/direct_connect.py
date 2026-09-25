#!/usr/bin/env python3
"""Place ``Direct``-tagged parts pad-on-pad against a pin already on their net.

A decoupling cap whose pad butts straight onto the IC pin it decouples, a
0-ohm link sitting on the end of a resistor, a TVS diode on a connector
pin: the connection is the placement. No trace, no via, no router in the
loop -- the copper of one pad touches the copper of the other. A placement
optimiser that minimises wirelength gets these *close*; it never gets them
*touching*, and "close" still leaves a trace for the router to get wrong.

So ``netlist.csv`` says so, per pin, in an optional ``Direct`` column::

    Net,RefDes,Pin,PinName,Direct,Note
    +3V3,U1,4,VDD,,
    +3V3,C5,1,~,yes,"100 nF on U1.4 -- pad on pad"
    GND,C5,2,~,,
    VBUS,D3,1,K,J1.A4,"TVS right on the connector pin"

``Direct`` values:

  * empty            an ordinary row (every existing netlist is unchanged)
  * ``yes`` / ``any`` / ``*``
                     find every OTHER pin already on this row's net and put
                     this part touching whichever one it fits against best
  * ``REF`` or ``REF.PIN``
                     the same search, restricted to that part (or that pin)

One tagged pin per part: the tagged pad is the one that touches, and the
rest of the part points away from the target. A part cannot be tagged and
also sit in ``[[floorplan.place]]`` -- that would be two positions for one
part, and nothing would say which wins. A tagged part cannot be the target of
another tagged part either: chains make the answer depend on processing order.

What "touching" means here
--------------------------
The tagged pad is aligned centre-to-centre with the target pad along one of
its four sides and pushed ``overlap_mm`` into it, so the two pads' copper
overlaps on a shared copper layer. Every candidate -- each target pad on the
net x 4 sides x 4 rotations -- is measured with pcbnew itself (the part is
actually rotated and its pads re-read, never rotated by hand-rolled trig) and
rejected if:

  * any other pad of the part comes within ``clearance_mm`` of a pad on
    another net (the target's neighbouring pins included),
  * any other pad of the part lands inside the target's courtyard (a cap
    between the pad rows of an SOIC is "not overlapping pins" and still
    physically under the IC body),
  * the part's courtyard leaves the board outline,
  * the target pad is not at a multiple of 90 degrees, or the two pads share
    no copper layer.

Survivors are ranked by courtyard collisions with other parts, then by
whether the target is the one the schematic draws (``preferred``: a named
pin, else a ``power_in`` pin from ``[[libs.symbols]]``, else the part with
the most netlist rows -- a decap goes on its IC's supply pin, not on a sense
pin or the next decap along the rail; a different pick is warned about and
reported as ``schematic_target``), then by
courtyard overlap with the target (body pointing away wins), then by
distance from where the part already sits -- which is what makes a re-run
choose the same answer. A best candidate that still collides with another
part is a FAILURE, not a warning: the fix is almost always to put the target
in ``[[floorplan.place]]`` so the part is held and the optimiser packs around
it (see Stages).

After the write the board is reloaded from disk and every pair is checked
independently: same net on both pads, pad shapes colliding on a shared
copper layer. The pass's own arithmetic is not the evidence.

Stages
------
``--stage pre`` runs after ``add_mounting_holes`` and before ``place.py``.
It places only parts that have at least one candidate target on an anchored
part (``[[floorplan.place]]``, mounting holes, ``[floorplan] anchors``) and
restricts them to those targets. ``place.py`` adds exactly these parts to
its anchor list (``held_refs`` below, imported so the two cannot drift), so
the optimiser packs around a part that is already in its final spot.

``--stage post`` runs after ``flip_sides.py``. It (re)places every tagged
part: held parts against their anchored targets again (a no-op unless a
target was flipped to the back, in which case the part follows it), the
rest against wherever the optimiser left their targets. Only parts are
considered obstacles that are final at that stage: anchored parts at
``pre``, everything at ``post`` -- plus tagged parts already placed earlier
in the same run, never tagged parts still waiting, so the order parts are
visited in cannot change the answer.

Expected DRC fallout
--------------------
The part's courtyard overlaps its target's courtyard. That is the design,
and KiCad DRC reports it as ``courtyards_overlap`` for every pair. The pairs
are listed in this pass's JSON (``courtyard_overlaps``) so the DRC review
can check the report against them -- a courtyard overlap between parts NOT
on that list is still a real finding.

Idempotent: a re-run over the same board finds the same candidates, the
current position wins the distance tie-break, and the pose is re-asserted.
Nothing is appended -- the pass moves footprints and adds no copper.

board.toml schema
-----------------
::

    [direct]
    overlap_mm   = 0.10   # how far the tagged pad reaches into the target pad
    clearance_mm = 0.20   # min gap from the part's other pads to other nets;
                          # default: the board's default netclass clearance

    [[pin_alias]]         # honoured: a netlist pin id mapped to its pad
    ref = "D1"
    pin = "K"
    lib_pin = "1"

Needs ``pcbnew`` (KiCad 9/10). The netlist helpers at the top of this file
do not, so ``place.py`` can import them under the kicad-tools interpreter.
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import _lib

NAME = "direct_connect"

COLUMN = "Direct"
ANY = {"yes", "y", "any", "*", "true", "1"}
DEFAULT_OVERLAP_MM = 0.10
DEFAULT_CLEARANCE_MM = 0.20
ROTATIONS = (0.0, 90.0, 180.0, 270.0)
SIDES = ("left", "right", "up", "down")  # KiCad page frame: "up" is -Y


# --------------------------------------------------------------------------
# netlist (no pcbnew below this line until the geometry section)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tag:
    ref: str
    pin: str
    net: str
    spec: str  # the raw Direct value
    lineno: int
    candidates: tuple[tuple[str, str], ...]  # (ref, pin) on the same net


def read_table(path: Path) -> list[tuple[int, dict]]:
    """[(lineno, row)] with a real CSV reader -- Note is quoted and may hold
    commas, so the first-three-fields split the other passes use is not
    enough to reach a column after PinName."""
    if not path.exists():
        _lib.fail(f"{path}: netlist not found")
    lines = path.read_text(encoding="utf-8").splitlines()
    body = [(i, l) for i, l in enumerate(lines, 1) if l.strip() and not l.lstrip().startswith("#")]
    if not body or not body[0][1].startswith("Net,"):
        _lib.fail(f"{path}: first non-comment line must be the Net,RefDes,Pin,... header")
    header = next(csv.reader([body[0][1]]))
    out: list[tuple[int, dict]] = []
    for lineno, line in body[1:]:
        if line.startswith("Net,"):
            continue
        fields = next(csv.reader([line]))
        row = {h.strip(): (fields[k].strip() if k < len(fields) else "") for k, h in enumerate(header)}
        out.append((lineno, row))
    return out


def pad_number(cfg: dict, ref: str, pin: str) -> str:
    """Netlist pin id -> footprint pad number, through [[pin_alias]]."""
    for e in cfg.get("pin_alias") or []:
        if e.get("ref") == ref and str(e.get("pin")) == pin and e.get("lib_pin"):
            return str(e["lib_pin"])
    return pin


def load_tags(netlist: Path, cfg: dict) -> list[Tag]:
    """Every Direct-tagged row, validated, with its candidate targets.

    A netlist with no Direct column is not an error: it has no tags.
    """
    rows = read_table(Path(netlist))
    floorplan = {
        str((e or {}).get("ref", "")).strip()
        for e in ((cfg.get("floorplan") or {}).get("place") or [])
    }

    raw: list[tuple[int, str, str, str, str]] = []
    rows_per_ref: dict[str, int] = {}
    by_net: dict[str, list[tuple[str, str]]] = {}
    for lineno, row in rows:
        net, ref, pin = row.get("Net", ""), row.get("RefDes", ""), row.get("Pin", "")
        if not net or not ref or not pin:
            continue  # the build's own readers report malformed rows
        by_net.setdefault(net, []).append((ref, pin))
        rows_per_ref[ref] = rows_per_ref.get(ref, 0) + 1
        spec = row.get(COLUMN, "")
        if spec:
            raw.append((lineno, net, ref, pin, spec))

    tagged_refs: dict[str, int] = {}
    for lineno, net, ref, pin, spec in raw:
        where = f"{netlist}:{lineno} ({ref}.{pin})"
        if net == "NC":
            _lib.fail(f"{where}: {COLUMN}={spec!r} on an NC row -- there is nothing to touch")
        if ref in tagged_refs:
            _lib.fail(
                f"{where}: {ref} already has a {COLUMN} pin on line {tagged_refs[ref]} -- "
                "one touching pad per part; the rest of the part points away from it"
            )
        if ref in floorplan:
            _lib.fail(
                f"{where}: {ref} is in [[floorplan.place]] AND tagged {COLUMN} -- two "
                "positions for one part. Drop one of them."
            )
        tagged_refs[ref] = lineno

    tags: list[Tag] = []
    for lineno, net, ref, pin, spec in raw:
        where = f"{netlist}:{lineno} ({ref}.{pin})"
        want_ref = want_pin = None
        if spec.lower() not in ANY:
            want_ref, _, want_pin = spec.partition(".")
            want_ref, want_pin = want_ref.strip(), (want_pin.strip() or None)
        cands = []
        for r, p in by_net.get(net, []):
            if r == ref:
                continue
            if want_ref and (r != want_ref or (want_pin and p != want_pin)):
                continue
            if r in tagged_refs:
                if want_ref:
                    _lib.fail(f"{where}: target {spec} is itself tagged {COLUMN} -- no chains")
                continue
            cands.append((r, p))
        if not cands:
            what = f"{spec} on net {net}" if want_ref else f"other pin on net {net}"
            _lib.fail(f"{where}: {COLUMN}={spec!r} but netlist.csv has no {what} to touch")
        # Preference order, which the schematic draws and the board tries
        # first: a named pin; else a pin [[libs.symbols]] declares power_in
        # (the buck's VIN, not the current monitor's sense pin on the same
        # rail); then the part with the most rows in netlist.csv (the IC a
        # decap serves, not the next decap along the rail); netlist order on
        # a tie.
        def rank(c: tuple[str, str]) -> tuple[int, int]:
            kind = _lib.pin_types(cfg, c[0]).get(pad_number(cfg, *c), "")
            return (0 if kind == "power_in" else 1, -rows_per_ref[c[0]])

        cands.sort(key=rank)
        tags.append(Tag(ref, pin, net, spec, lineno, tuple(cands)))
    return tags


def is_held(tag: Tag, anchors: set[str]) -> bool:
    """A tag with an anchored candidate is placed before the optimiser runs
    and held there. MUST be the one rule both this pass and place.py use."""
    return any(r in anchors for r, _ in tag.candidates)


def preferred(tag: Tag, anchors: set[str]) -> tuple[str, str]:
    """The target generate_schematic draws the part against, and the one this
    pass picks whenever it fits. MUST be the one rule both use: the schematic
    a human reviews is the board that ships."""
    held = is_held(tag, anchors)
    return next(c for c in tag.candidates if not held or c[0] in anchors)


def anchor_set(cfg: dict) -> set[str]:
    """place.anchors_for without its failure: a board with no anchors yet
    fails in place.py, where the message about anchors belongs."""
    from place import anchors_for  # lazy: place imports this module

    try:
        return set(anchors_for(cfg))
    except SystemExit:
        return set()


def held_refs(netlist: Path, cfg: dict, anchors: list[str] | set[str]) -> list[str]:
    """Refs place.py must add to its anchor list. Imported by place.py."""
    a = set(anchors)
    return [t.ref for t in load_tags(netlist, cfg) if is_held(t, a)]


# --------------------------------------------------------------------------
# geometry -- plain rectangles in KiCad page mm (Y down)
# --------------------------------------------------------------------------

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


def shift(r: Rect, dx: float, dy: float) -> Rect:
    return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)


def gap(a: Rect, b: Rect) -> float:
    """Edge-to-edge distance; negative when the rectangles overlap."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx < 0 and dy < 0:
        return max(dx, dy)
    return math.hypot(max(dx, 0.0), max(dy, 0.0))


def overlap_area(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def inside(inner: Rect, outer: Rect, eps: float = 1e-6) -> bool:
    return (inner[0] >= outer[0] - eps and inner[1] >= outer[1] - eps
            and inner[2] <= outer[2] + eps and inner[3] <= outer[3] + eps)


def snap_offset(p_rel: Rect, target: Rect, side: str, overlap: float) -> tuple[float, float]:
    """Footprint position that puts the tagged pad (``p_rel``, relative to
    the footprint origin) against ``side`` of ``target``, centres aligned,
    reaching ``overlap`` mm into it."""
    tcx, tcy = (target[0] + target[2]) / 2, (target[1] + target[3]) / 2
    pcx, pcy = (p_rel[0] + p_rel[2]) / 2, (p_rel[1] + p_rel[3]) / 2
    if side == "right":
        return target[2] - overlap - p_rel[0], tcy - pcy
    if side == "left":
        return target[0] + overlap - p_rel[2], tcy - pcy
    if side == "up":
        return tcx - pcx, target[1] + overlap - p_rel[3]
    if side == "down":
        return tcx - pcx, target[3] - overlap - p_rel[1]
    raise ValueError(side)


@dataclass
class Probe:
    """The part measured at one rotation, relative to its origin."""
    rotation: float
    pads: list[tuple[str, str, frozenset, Rect]]  # (number, net, sides, rect)
    court: Rect


@dataclass
class Obstacle:
    ref: str
    court: Rect
    pads: list[tuple[str, frozenset, Rect]]  # (net, sides, rect)


def evaluate(
    probe: Probe,
    tag_pad: str,
    target_ref: str,
    target_rect: Rect,
    target_sides: frozenset,
    side: str,
    obstacles: list[Obstacle],
    outline: Rect,
    overlap: float,
    clearance: float,
    current: tuple[float, float],
):
    """(score, pos, collisions) for one candidate, or (None, reason)."""
    mine = [p for p in probe.pads if p[0] == tag_pad]
    if len(mine) != 1:
        return None, f"{len(mine)} pads numbered {tag_pad}"
    _, _, p_sides, p_rel = mine[0]
    if not (p_sides & target_sides):
        return None, "no shared copper layer"
    x, y = snap_offset(p_rel, target_rect, side, overlap)
    court = shift(probe.court, x, y)
    if not inside(court, outline):
        return None, "courtyard off board"

    tgt = next((o for o in obstacles if o.ref == target_ref), None)
    collisions: list[str] = []
    for num, net, sides, rel in probe.pads:
        r = shift(rel, x, y)
        is_tag = num == tag_pad
        if not is_tag and tgt and overlap_area(r, tgt.court) > 0:
            return None, "pad under the target's body"
        for o in obstacles:
            for onet, osides, orect in o.pads:
                if not (sides & osides) or onet == net:
                    continue
                if gap(r, orect) < clearance - 1e-6:
                    return None, f"pad {num} within {clearance} mm of {o.ref} ({onet})"
    for o in obstacles:
        if o.ref != target_ref and overlap_area(court, o.court) > 0:
            collisions.append(o.ref)
    body = overlap_area(court, tgt.court) if tgt else 0.0
    dist = math.hypot(x - current[0], y - current[1])
    return ((len(collisions), round(body, 4), round(dist, 4)), (x, y), collisions), ""


# --------------------------------------------------------------------------
# pcbnew
# --------------------------------------------------------------------------


def main() -> int:
    ap = _lib.pass_parser(NAME)
    ap.add_argument("--stage", choices=("pre", "post"), required=True,
                    help="pre: before place.py, anchored targets only; post: after flip_sides, all")
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found")
    cfg = _lib.load_config(args.config)
    tags = load_tags(Path(args.netlist), cfg)
    if not tags:
        print(f"  no {COLUMN} tags in {args.netlist} -- nothing to place", file=sys.stderr)
        _lib.emit(NAME, board=str(board), stage=args.stage, placed=[],
                  nets=_lib.assert_net_table(board))
        return 0

    from place import anchors_for  # lazy: place imports this module

    anchors = set(anchors_for(cfg))
    work = [t for t in tags if args.stage == "post" or is_held(t, anchors)]
    deferred = [t.ref for t in tags if t not in work]

    import pcbnew
    from check_placement import courtyard_rect

    dcfg = cfg.get("direct") or {}
    overlap = float(dcfg.get("overlap_mm", DEFAULT_OVERLAP_MM))
    if overlap <= 0:
        _lib.fail("[direct] overlap_mm must be > 0 -- pads that merely share an edge are not "
                  "reliably connected in every tool that reads the board")

    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew returned None loading {board}")
    if "clearance_mm" in dcfg:
        clearance = float(dcfg["clearance_mm"])
    else:
        try:
            clearance = pcbnew.ToMM(b.GetDesignSettings().GetDefault().GetClearance())
        except Exception:  # noqa: BLE001 - SWIG surface varies across KiCad majors
            clearance = DEFAULT_CLEARANCE_MM

    mm = pcbnew.ToMM

    def rect(box) -> Rect:
        return (mm(box.GetLeft()), mm(box.GetTop()), mm(box.GetRight()), mm(box.GetBottom()))

    def sides_of(pad) -> frozenset:
        return frozenset(s for s, lid in (("F", pcbnew.F_Cu), ("B", pcbnew.B_Cu)) if pad.IsOnLayer(lid))

    def court_of(fp) -> Rect:
        if hasattr(fp, "BuildCourtyardCaches"):
            fp.BuildCourtyardCaches()
        box, _ = courtyard_rect(fp)
        return tuple(mm(v) for v in box)  # rect_of is (x0, y0, x1, y1) in nm

    def obstacle(fp) -> Obstacle:
        return Obstacle(
            fp.GetReference(),
            court_of(fp),
            [(p.GetNetname(), sides_of(p), rect(p.GetBoundingBox())) for p in fp.Pads()],
        )

    by_ref = {fp.GetReference(): fp for fp in b.GetFootprints()}
    missing = sorted({t.ref for t in work} - set(by_ref))
    if missing:
        _lib.fail(f"{COLUMN}-tagged refs not on board: {missing}")

    outline = rect(b.GetBoardEdgesBoundingBox())
    tagged = {t.ref for t in tags}
    final = (anchors if args.stage == "pre" else set(by_ref)) - tagged
    obstacles = [obstacle(by_ref[r]) for r in sorted(final) if r in by_ref]
    frame = _lib.board_frame(b)

    placed: list[dict] = []
    for t in sorted(work, key=lambda t: (t.ref[0], len(t.ref), t.ref)):
        fp = by_ref[t.ref]
        tag_pad = pad_number(cfg, t.ref, t.pin)
        held = is_held(t, anchors)
        cands = [c for c in t.candidates if not held or c[0] in anchors]
        pref = preferred(t, anchors)

        best = None
        reasons: dict[str, int] = {}
        start = (mm(fp.GetPosition().x), mm(fp.GetPosition().y))
        start_rot = fp.GetOrientationDegrees()
        for tref, tpin in cands:
            tfp = by_ref.get(tref)
            if tfp is None:
                _lib.fail(f"{t.ref}: candidate target {tref} is in netlist.csv but not on the board")
            tnum = pad_number(cfg, tref, tpin)
            tpads = [p for p in tfp.Pads() if p.GetNumber() == tnum]
            if not tpads:
                _lib.fail(f"{t.ref}: target {tref} has no pad numbered {tnum!r} "
                          f"(netlist pin {tpin!r}; add a [[pin_alias]] if the ids differ)")
            for tp in tpads:
                if tp.GetNetname() != t.net:
                    _lib.fail(f"{tref} pad {tnum} is on net {tp.GetNetname()!r} on the board, "
                              f"{t.net!r} in netlist.csv -- rebuild from the netlist")
                if abs(math.remainder(tp.GetOrientationDegrees(), 90.0)) > 1e-3:
                    reasons["target pad not at a multiple of 90 deg"] = reasons.get(
                        "target pad not at a multiple of 90 deg", 0) + 1
                    continue
                trect, tsides = rect(tp.GetBoundingBox()), sides_of(tp)
                # An SMD target on the back takes the part with it.
                want_back = tsides == frozenset({"B"})
                want_front = tsides == frozenset({"F"})
                if (want_back and not fp.IsFlipped()) or (want_front and fp.IsFlipped()):
                    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
                others = [o for o in obstacles if o.ref != t.ref]
                for rot in ROTATIONS:
                    fp.SetOrientationDegrees(rot)
                    pos = fp.GetPosition()
                    ox, oy = mm(pos.x), mm(pos.y)
                    probe = Probe(
                        rot,
                        [(p.GetNumber(), p.GetNetname(), sides_of(p),
                          shift(rect(p.GetBoundingBox()), -ox, -oy)) for p in fp.Pads()],
                        shift(court_of(fp), -ox, -oy),
                    )
                    for side in SIDES:
                        res, why = evaluate(probe, tag_pad, tref, trect, tsides, side, others,
                                            outline, overlap, clearance, start)
                        if res is None:
                            reasons[why] = reasons.get(why, 0) + 1
                            continue
                        score, (x, y), coll = res
                        # Collisions first, then the schematic's target, then fit.
                        key = (score[0], 0 if (tref, tpin) == pref else 1, score[1:],
                               0 if rot == start_rot else 1, SIDES.index(side), rot)
                        if best is None or key < best[0]:
                            best = (key, (x, y), rot, tref, tnum, side, coll, fp.IsFlipped(), tpin)

        if best is None:
            why = "; ".join(f"{n}x {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
            _lib.fail(f"{t.ref}.{t.pin}: no pose touches any of {[f'{r}.{p}' for r, p in cands]} "
                      f"without a clearance or outline problem ({why})")
        _, (x, y), rot, tref, tnum, side, coll, flipped, tpin = best
        if (tref, tpin) != pref:
            print(f"  WARNING {t.ref}.{t.pin}: the schematic draws it on {pref[0]}.{pref[1]} but "
                  f"that pin has no free pose; placed on {tref}.{tpin} (same net {t.net}). "
                  f"Name the target in the {COLUMN} column to make the choice yours.",
                  file=sys.stderr)
        if coll:
            _lib.fail(
                f"{t.ref}.{t.pin}: the best pose against {tref}.{tnum} ({side}) still overlaps "
                f"{coll}. Put {tref} in [[floorplan.place]] so {t.ref} is placed before the "
                "optimiser and held, or move what is in the way."
            )
        if fp.IsFlipped() != flipped:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        fp.SetLocked(True)
        obstacles.append(obstacle(fp))  # later tags must respect this one
        bx, by = x - frame[0], frame[1] - y
        placed.append({
            "ref": t.ref, "pin": t.pin, "target": f"{tref}.{tnum}", "side": side,
            "rotation": rot, "x": round(bx, 3), "y": round(by, 3),
            "layer": "B" if flipped else "F", "held": held,
            "schematic_target": f"{pref[0]}.{pad_number(cfg, *pref)}",
        })
        print(f"  {t.ref}.{t.pin} -> {tref}.{tnum} ({side}, rot {rot:g}, "
              f"board ({bx:.3f}, {by:.3f}){', held' if held else ''})", file=sys.stderr)

    if placed:
        pcbnew.SaveBoard(str(board), b)
    nets = _lib.assert_net_table(board)

    # Independent check, off the file just written: same net, copper touching.
    if placed:
        v = pcbnew.LoadBoard(str(board))
        vref = {fp.GetReference(): fp for fp in v.GetFootprints()}
        for p in placed:
            tref, tnum = p["target"].split(".", 1)
            tag = next(t for t in work if t.ref == p["ref"])
            num = pad_number(cfg, tag.ref, tag.pin)
            a = [q for q in vref[tag.ref].Pads() if q.GetNumber() == num]
            z = [q for q in vref[tref].Pads() if q.GetNumber() == tnum]
            ok = False
            for pa in a:
                for pz in z:
                    if pa.GetNetname() != tag.net or pz.GetNetname() != tag.net:
                        continue
                    for lid in (pcbnew.F_Cu, pcbnew.B_Cu):
                        if not (pa.IsOnLayer(lid) and pz.IsOnLayer(lid)):
                            continue
                        try:
                            hit = pa.GetEffectiveShape(lid).Collide(pz.GetEffectiveShape(lid), 0)
                        except Exception:  # noqa: BLE001 - fall back to boxes
                            hit = gap(rect(pa.GetBoundingBox()), rect(pz.GetBoundingBox())) < 0
                        ok = ok or bool(hit)
            if not ok:
                _lib.fail(f"verify: {p['ref']}.{tag.pin} and {p['target']} do not share copper on "
                          f"net {tag.net} in the saved board -- the placement did not take")

    _lib.emit(
        NAME,
        board=str(board),
        stage=args.stage,
        overlap_mm=overlap,
        clearance_mm=round(clearance, 4),
        placed=placed,
        deferred_to_post=deferred,
        courtyard_overlaps=[[p["ref"], p["target"].split(".", 1)[0]] for p in placed],
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
