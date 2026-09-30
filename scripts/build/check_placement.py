#!/usr/bin/env python3
"""Board-level placement checks: connector accessibility, copper in keepouts.

Both checks answer questions that only become expensive later.  A connector
you cannot plug into and a pad sitting inside the antenna keepout are
*placement* facts, decided the moment ``place.py`` finishes -- but nothing in
this pipeline looked at them until ``validate_gerbers.py`` read the exported
gerbers, which is a build, a route and an export away (~15 minutes), and in
the connector's case never at all.  This pass reads the board directly and
answers both in about a second, so a floorplan mistake is caught while the
floorplan is still the thing you are editing.

It writes nothing.  It reads ``--board``, reports, and exits non-zero on a
failure (or zero with ``--warn-only``, for running it mid-build where the
board is not final yet).

Relationship to validate_gerbers.py
-----------------------------------
The keepout check here and ``check_keepouts`` there are deliberately BOTH
kept.  They test different artifacts and fail differently: this one sees named
board objects (``R12 pad 2``, ``via at ...``, ``zone GND``) and can say *what*
is in the keepout, but it trusts the board file; the gerber check sees only
apertures and flashes and cannot name them, but it is reading the bytes the
fab will actually plot.  A pass here and a fail there means the export moved
something -- which is the whole reason a level-4 checker exists.  Neither
replaces the other, and this one running green is not grounds for skipping
``make check``.

board.toml
----------
Connectors are opt-in; a board that declares none reports SKIP, not PASS::

    [[connectors]]
    ref               = "J1"      # required
    face              = "bottom"  # board-frame edge the plug enters from:
                                  # left/right/top/bottom, or "auto" (nearest)
    mating_depth_mm   = 8.0       # clear space the plug/boot needs in front
    edge_offset_mm    = 0.5       # max gap from the mating face to the outline
    lateral_margin_mm = 0.5       # widen the corridor each side (shell, boot)
    keep_clear_mm     = 0.0       # extra on-board clearance behind the face
    accept_blockers   = ["H3"]    # refs whose presence in the corridor is
                                  # ACKNOWLEDGED, not fixed -- see below

    [mounting_holes]
    head_diameter_mm  = 6.0       # optional; the SCREW HEAD, not the hole

``accept_blockers`` is the escape hatch for an obstruction you cannot move:
the example board's screw terminals sit beside mounting holes whose positions
are fixed by the Arduino UNO outline.  An accepted blocker is still measured,
still printed (as ``accepted:``) and still in the JSON -- it is downgraded,
never hidden.  A ref listed there that is NOT in the corridor is itself a
FAILURE: a stale acceptance is a check that succeeds forever, which is worse
than no check.

Keepouts reuse the ``[[keepouts]]`` rectangles ``add_keepouts.py`` already
consumes -- same rectangles, same board frame, same ``allow_*`` flags, plus
one this pass adds::

    allow_pads = false            # a keepout does not ban pads in KiCad DRC,
                                  # so nothing but a checker ever catches one

Coordinates
-----------
board.toml is board-frame throughout: origin at the outline's BOTTOM-LEFT
corner, X right, **Y UP**.  KiCad pages run Y DOWN.  Every rectangle and
every face name is converted through ``_lib.board_frame`` / ``to_kicad_xy``,
so "bottom" here means the edge at board-frame y = 0 -- the low edge of the
board as you look at it in the fab render, which is KiCad's *maximum* y.
Getting this backwards checks the mirror image of what you meant and passes.

Why the screw head and not the hole
-----------------------------------
A mounting hole's footprint courtyard is the hole, and by that measure a USB-C
receptacle 3 mm from an M3 hole is unobstructed.  The thing in the way is the
screw head -- 5.5-6 mm across on an M3 pan head, i.e. 1.4-1.7 mm of overhang
in every direction that no courtyard in the file describes.  Holes named by
``[mounting_holes]`` are therefore inflated to ``head_diameter_mm`` before the
corridor test.

Usage::

    python3 check_placement.py --board board.kicad_pcb --config board.toml
    python3 check_placement.py --board board.kicad_pcb --config board.toml --warn-only
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import pcbnew

import _lib
from add_mounting_holes import hole_refs

PASS = "check_placement"

FACES = ("left", "right", "top", "bottom")
DEFAULT_MATING_DEPTH_MM = 8.0
DEFAULT_EDGE_OFFSET_MM = 0.5
DEFAULT_LATERAL_MARGIN_MM = 0.5
DEFAULT_HEAD_DIAMETER_MM = 6.0

# Strict interior, in mm.  A pour correctly carved around a keepout leaves
# polygon vertices exactly ON the boundary; flagging those would fail every
# fill that did the right thing.  Copper 1 um inside still fails.  Same
# epsilon and same reasoning as validate_gerbers.check_keepouts.
EPS_MM = 1e-3


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


class Report:
    """PASS/FAIL/SKIP lines, mirroring validate_gerbers' vocabulary."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def _add(self, status: str, name: str, detail: str) -> None:
        self.rows.append({"check": name, "status": status, "detail": detail})
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"  {mark:4}  {name}: {detail}", file=sys.stderr)

    def ok(self, name: str, detail: str) -> None:
        self._add("pass", name, detail)

    def bad(self, name: str, detail: str) -> None:
        self._add("fail", name, detail)

    def skip(self, name: str, detail: str) -> None:
        self._add("skip", name, detail)

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "fail")


# --------------------------------------------------------------------------
# geometry, all in KiCad internal units unless a name says _mm
# --------------------------------------------------------------------------


def rect_of(box) -> tuple[int, int, int, int]:
    """BOX2I -> (l, t, r, b) plain ints, safe across SWIG mutations."""
    return (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom())


def rects_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def overlap_area_mm2(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return pcbnew.ToMM(w) * pcbnew.ToMM(h)


def seg_hits_rect(x1, y1, x2, y2, r) -> bool:
    """Exact segment vs axis-aligned rect (Liang-Barsky), not a sampling test.

    The gerber checker samples along the segment every 0.2 mm because it is
    reading flattened plot geometry; here the endpoints are exact, so a
    clipping test is both cheaper and incapable of stepping over a thin
    keepout the way sampling can.
    """
    if r[0] <= x1 <= r[2] and r[1] <= y1 <= r[3]:
        return True
    if r[0] <= x2 <= r[2] and r[1] <= y2 <= r[3]:
        return True
    t0, t1 = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for p, q in ((-dx, x1 - r[0]), (dx, r[2] - x1), (-dy, y1 - r[1]), (dy, r[3] - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def via_radius(via, lids) -> int:
    """Widest annulus this via presents on any of ``lids``, in KiCad units.

    KiCad 10 made ``PCB_VIA::GetWidth()`` per-layer and asserts loudly on the
    no-argument form (padstacks can differ layer to layer); KiCad 9 has only
    the no-argument form.  Ask for the per-layer width, take the widest, and
    fall back to the old signature.
    """
    best = 0
    for lid in lids:
        try:
            best = max(best, int(via.GetWidth(lid)))
        except TypeError:
            return int(via.GetWidth()) // 2
    return best // 2


def is_rule_area(z) -> bool:
    """KiCad 9 spells it ``IsRuleArea``; KiCad 10's SWIG only exposes the
    getter.  Guessing wrong throws, so ask the object."""
    fn = getattr(z, "IsRuleArea", None) or getattr(z, "GetIsRuleArea", None)
    return bool(fn()) if fn else False


def courtyard_rect(fp):
    """A footprint's courtyard bbox, and whether it really had one.

    A part with no courtyard is not a part with no extent -- several JLC
    footprints ship without one -- so the fallback to the footprint bbox is
    deliberate, and the caller reports which parts needed it rather than
    quietly measuring something else.
    """
    side = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
    try:
        court = fp.GetCourtyard(side)
        if court.OutlineCount() > 0:
            return rect_of(court.BBox()), True
    except Exception:  # noqa: BLE001 - SWIG surface varies across KiCad majors
        pass
    return rect_of(fp.GetBoundingBox(False)), False


# --------------------------------------------------------------------------
# connector accessibility
# --------------------------------------------------------------------------


def kicad_face(face: str) -> str:
    """board-frame face name -> which side of the KiCad bbox it is.

    Board frame is Y UP, KiCad is Y DOWN, so the two vertical names swap and
    the two horizontal ones do not.
    """
    return {"left": "xmin", "right": "xmax", "bottom": "ymax", "top": "ymin"}[face]


def nearest_face(part, board_box) -> str:
    """The board-frame face this part sits closest to."""
    gaps = {
        "left": part[0] - board_box[0],
        "right": board_box[2] - part[2],
        "top": part[1] - board_box[1],       # KiCad ymin side = board-frame top
        "bottom": board_box[3] - part[3],    # KiCad ymax side = board-frame bottom
    }
    return min(gaps, key=lambda k: gaps[k])


def face_gap_mm(part, board_box, face: str) -> float:
    """Distance from the connector's mating face to the board edge, in mm.

    Negative means the part overhangs the outline -- normal and correct for a
    USB-C receptacle, so it is reported as a note, not an error.
    """
    side = kicad_face(face)
    if side == "xmin":
        return pcbnew.ToMM(part[0] - board_box[0])
    if side == "xmax":
        return pcbnew.ToMM(board_box[2] - part[2])
    if side == "ymin":
        return pcbnew.ToMM(part[1] - board_box[1])
    return pcbnew.ToMM(board_box[3] - part[3])


def corridor(part, board_box, face: str, depth: int, lateral: int, behind: int):
    """The volume the plug and its cable need, as a rect in KiCad units.

    It runs from ``behind`` inside the connector's own mating face, out
    through the board edge, to ``depth`` beyond it -- the off-board length
    matters because an L-shaped outline or a part on a neighbouring tab can
    still be in the way of the plug body.
    """
    side = kicad_face(face)
    if side in ("xmin", "xmax"):
        lo, hi = part[1] - lateral, part[3] + lateral
        if side == "xmin":
            return (board_box[0] - depth, lo, part[0] + behind, hi)
        return (part[2] - behind, lo, board_box[2] + depth, hi)
    lo, hi = part[0] - lateral, part[2] + lateral
    if side == "ymin":
        return (lo, board_box[1] - depth, hi, part[1] + behind)
    return (lo, part[3] - behind, hi, board_box[3] + depth)


LOCAL_DIRS = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}
LIB_PAD_RE = re.compile(
    r'\(pad\s+(?:"([^"]*)"|(\S+))\s+\S+\s+\S+\s*\(at\s+([-\d.]+)\s+([-\d.]+)'
)
DEFAULT_PREFIXES = ["J", "P", "CN", "USB"]


def library_pad_positions(lib: Path, fp_name: str) -> dict[str, tuple[float, float]] | None:
    """pad number -> library-local (x, y) mm, for numbers that occur once."""
    path = lib / f"{fp_name.split(':')[-1]}.kicad_mod"
    if not path.exists():
        return None
    seen: dict[str, list[tuple[float, float]]] = {}
    for m in LIB_PAD_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        num = m.group(1) if m.group(1) is not None else m.group(2)
        seen.setdefault(num, []).append((float(m.group(3)), float(m.group(4))))
    return {n: v[0] for n, v in seen.items() if len(v) == 1 and n}


def fit_library_to_board(pairs, flipped: bool):
    """Library-local -> board transform, fitted from the part's own pads.

    No rotation-sign or flip convention is assumed. Both hypotheses (a pure
    rotation, and a rotation after a mirror) are fitted by least squares to
    (library position, board position) pad pairs, and the smaller residual
    wins. If the pads are collinear (a single-row connector) the two fit
    equally well: a front-side part cannot be mirrored, so that case is
    decided by the side, and a back-side single-row part is undecidable
    from pads alone.

    Returns (apply(dx, dy) -> (wx, wy), note) or (None, why).
    """
    if len(pairs) < 2:
        return None, "fewer than 2 uniquely numbered pads to fit against"
    n = len(pairs)
    lcx = sum(p[0] for p in pairs) / n
    lcy = sum(p[1] for p in pairs) / n
    wcx = sum(p[2] for p in pairs) / n
    wcy = sum(p[3] for p in pairs) / n
    fits = []
    for mirror in (False, True):
        s_cos = s_sin = 0.0
        pts = []
        for lx, ly, wx, wy in pairs:
            x, y = lx - lcx, ly - lcy
            if mirror:
                x = -x
            u, v = wx - wcx, wy - wcy
            pts.append((x, y, u, v))
            s_cos += x * u + y * v
            s_sin += x * v - y * u
        th = math.atan2(s_sin, s_cos)
        c, s = math.cos(th), math.sin(th)
        resid = sum((x * c - y * s - u) ** 2 + (x * s + y * c - v) ** 2
                    for x, y, u, v in pts)
        fits.append((resid, mirror, c, s))
    fits.sort()
    best, other = fits
    if best[0] > 0.01 * n:
        return None, (f"board pads do not match the library footprint "
                      f"(fit residual {best[0]:.3f} mm2) -- corrupted pads? "
                      f"run fix_pad_angles/repair_pads")
    if other[0] - best[0] < 1e-6:           # collinear: mirror is invisible
        if flipped:
            return None, ("single-row part on the back side: the pads cannot tell "
                          "a mirror from a rotation")
        best = next(f for f in fits if not f[1])
    _r, mirror, c, s = best

    def apply(dx: float, dy: float) -> tuple[float, float]:
        if mirror:
            dx = -dx
        return (dx * c - dy * s, dx * s + dy * c)

    return apply, ("mirrored" if mirror else "")


def world_face(wx: float, wy: float) -> str | None:
    """KiCad-frame direction (Y down) -> board-frame face name, or None if diagonal."""
    if abs(abs(wx) - abs(wy)) < 0.2:
        return None
    if abs(wx) > abs(wy):
        return "right" if wx > 0 else "left"
    return "bottom" if wy > 0 else "top"      # KiCad +Y is board-frame down


def connector_opening(fp, cfg: dict, lib: Path | None):
    """Board-frame face the plug enters from, derived from the part's rotation.

    Returns (face, detail) or (None, reason). The opening is a property of
    the FOOTPRINT, read off its datasheet land drawing, and lives in
    ``[connector_footprints."<name>"] opening = "+y"`` (library-local axes).
    """
    fname = str(fp.GetFPID().GetLibItemName())
    table = cfg.get("connector_footprints") or {}
    spec = table.get(fname)
    if not spec or not spec.get("opening"):
        return None, (f'no opening direction for footprint "{fname}" -- read it '
                      f'off the datasheet land drawing and add [connector_footprints.'
                      f'"{fname}"] opening = "<+x|-x|+y|-y>" (library-local), '
                      f'with the page it came from')
    d = str(spec["opening"]).lower()
    if d not in LOCAL_DIRS:
        _lib.fail(f'[connector_footprints."{fname}"] opening={d!r}; expected '
                  f'{", ".join(LOCAL_DIRS)}')
    if lib is None:
        return None, "no footprint library to fit the part's rotation against"
    lib_pos = library_pad_positions(lib, fname)
    if not lib_pos:
        return None, f"{fname}.kicad_mod not found in {lib}"
    pairs = []
    for pad in fp.Pads():
        num = pad.GetNumber()
        if num in lib_pos:
            pos = pad.GetPosition()
            pairs.append((*lib_pos[num], pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)))
    apply, note = fit_library_to_board(pairs, fp.IsFlipped())
    if apply is None:
        return None, note
    face = world_face(*apply(*LOCAL_DIRS[d]))
    if face is None:
        return None, "rotated off-axis (not a multiple of 90 deg) -- no board edge to face"
    return face, f"opening {d} in the library -> {face}" + (f" ({note})" if note else "")


def check_connectors(b, cfg: dict, rep: Report, board_path: Path | None = None) -> list[dict]:
    conns = cfg.get("connectors") or []
    check_undeclared(b, cfg, rep)
    if not conns:
        rep.skip(
            "connectors",
            "no [[connectors]] declared -- nothing checks that a plug fits",
        )
        return []
    lib = None
    if board_path is not None and cfg.get("connector_footprints"):
        from fix_pad_angles import footprint_lib
        lib = footprint_lib(cfg, board_path)

    FM = pcbnew.FromMM
    board_box = rect_of(b.GetBoardEdgesBoundingBox())
    holes = set(hole_refs(cfg))
    head_mm = float(
        (cfg.get("mounting_holes") or {}).get("head_diameter_mm", DEFAULT_HEAD_DIAMETER_MM)
    )

    parts: dict[str, tuple] = {}
    fps: dict[str, object] = {}
    side: dict[str, str] = {}
    no_courtyard: list[str] = []
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        fps[ref] = fp
        # holes are through the board: the screw head is on one side, the nut
        # or standoff on the other, so a hole blocks both
        side[ref] = "both" if ref in holes else ("bottom" if fp.IsFlipped() else "top")
        r, had = courtyard_rect(fp)
        if ref in holes:
            # The screw head, not the hole. See the module docstring.
            cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            half = FM(head_mm) // 2
            r = (cx - half, cy - half, cx + half, cy + half)
        elif not had:
            no_courtyard.append(ref)
        parts[ref] = r
    if no_courtyard:
        rep.skip(
            "connectors/courtyards",
            f"{len(no_courtyard)} footprint(s) have no courtyard, measured by "
            f"bounding box instead: {', '.join(sorted(no_courtyard)[:6])}",
        )

    out = []
    for i, c in enumerate(conns):
        where = f"[[connectors]] #{i + 1}"
        ref = str(c.get("ref") or "")
        if not ref:
            _lib.fail(f"{where} has no ref")
        name = f"connectors/{ref}"
        if ref not in parts:
            rep.bad(name, f"{where}: no footprint with this ref on the board")
            out.append({"ref": ref, "ok": False, "reason": "missing"})
            continue

        part = parts[ref]
        mating = str(c.get("mating", "edge")).lower()
        if mating == "vertical":
            out.append(check_vertical(ref, c, where, part, parts, side, holes, rep))
            continue
        if mating != "edge":
            _lib.fail(f"{where} mating={mating!r}; expected edge or vertical")

        # Which way does the part ACTUALLY face? Derived from its rotation, not
        # declared. A part rotated 180 deg at the right edge passes every
        # corridor test while its opening points into the board.
        opening, why = connector_opening(fps[ref], cfg, lib)
        face = str(c.get("face", "auto")).lower()
        if face == "auto":
            face = opening or nearest_face(part, board_box)
            auto = " (from rotation)" if opening else " (auto, nearest edge)"
        elif face not in FACES:
            _lib.fail(f"{where} face={face!r}; expected one of {', '.join(FACES)} or auto")
        else:
            auto = ""

        depth = FM(float(c.get("mating_depth_mm", DEFAULT_MATING_DEPTH_MM)))
        lateral = FM(float(c.get("lateral_margin_mm", DEFAULT_LATERAL_MARGIN_MM)))
        behind = FM(float(c.get("keep_clear_mm", 0.0)))
        max_gap = float(c.get("edge_offset_mm", DEFAULT_EDGE_OFFSET_MM))

        gap = face_gap_mm(part, board_box, face)
        corr = corridor(part, board_box, face, depth, lateral, behind)

        accepted_refs = [str(x) for x in (c.get("accept_blockers") or [])]

        blockers = []
        accepted = []
        for oref, orect in parts.items():
            if oref == ref:
                continue
            if not rects_overlap(corr, orect):
                continue
            area = overlap_area_mm2(corr, orect)
            kind = "screw head" if oref in holes else "part"
            (accepted if oref in accepted_refs else blockers).append((oref, kind, area))
        blockers.sort(key=lambda t: -t[2])
        accepted.sort(key=lambda t: -t[2])
        stale = [r_ for r_ in accepted_refs if r_ not in {a[0] for a in accepted}]

        detail = (
            f"face={face}{auto}, gap {gap:.2f} mm to the edge, "
            f"corridor {pcbnew.ToMM(depth):.1f} mm deep"
        )
        problems = []
        if opening is None:
            problems.append(f"orientation not verified: {why}")
        elif opening != face:
            problems.append(
                f"ROTATED: its opening faces {opening}, not the {face} edge "
                f"({why}) -- the plug would have to enter from inside the board"
            )
        else:
            detail += f"; {why}"
        if gap > max_gap:
            problems.append(
                f"sits {gap:.2f} mm inboard of the {face} edge (max "
                f"{max_gap:.2f}) -- the plug cannot reach it"
            )
        if blockers:
            problems.append(
                "mating corridor blocked by "
                + ", ".join(f"{r_} ({k}, {a:.1f} mm2)" for r_, k, a in blockers[:4])
            )
        if stale:
            problems.append(
                "accept_blockers names " + ", ".join(stale) + " but nothing of "
                "that ref is in the corridor -- a stale acceptance is a check "
                "that passes forever; drop it"
            )
        if accepted:
            detail += "; accepted: " + ", ".join(
                f"{r_} ({k}, {a:.1f} mm2)" for r_, k, a in accepted
            )
        if problems:
            rep.bad(name, detail + "; " + "; ".join(problems))
        elif gap < 0:
            rep.ok(name, detail + " (overhangs the outline, clear)")
        else:
            rep.ok(name, detail + ", clear")

        out.append(
            {
                "ref": ref,
                "mating": "edge",
                "face": face,
                "opening_face": opening,
                "edge_gap_mm": round(gap, 3),
                "blockers": [
                    {"ref": r_, "kind": k, "area_mm2": round(a, 2)} for r_, k, a in blockers
                ],
                "accepted": [
                    {"ref": r_, "kind": k, "area_mm2": round(a, 2)} for r_, k, a in accepted
                ],
                "ok": not problems,
            }
        )
    return out


def check_vertical(ref, c, where, part, parts, side, holes, rep: Report) -> dict:
    """A connector that mates from above or below: pin headers, top-entry
    sockets, the stacking headers of a shield.

    The plug (or the host board's socket) comes straight down onto the
    part, so what has to be clear is a ring around it on the MATING side:
    the plug housing, a finger to pull it, and a latch to press. Other
    courtyards on that side inside the ring block it. Mounting holes are
    inflated to the screw head and count on both sides. No height model
    exists, so a short part in the ring fails as readily as a tall one:
    accept it (``accept_blockers``) with the reason that it is low.

    ``mating_side`` is where the plug comes from: "top" for a header you
    plug a cable onto, "bottom" for a shield's pins that go down into the
    host board's sockets.
    """
    name = f"connectors/{ref}"
    mside = str(c.get("mating_side", "top")).lower()
    if mside not in ("top", "bottom"):
        _lib.fail(f"{where} mating_side={mside!r}; expected top or bottom")
    margin = pcbnew.FromMM(float(c.get("access_margin_mm", 1.0)))
    ring = (part[0] - margin, part[1] - margin, part[2] + margin, part[3] + margin)
    accepted_refs = [str(x) for x in (c.get("accept_blockers") or [])]
    blockers, accepted = [], []
    for oref, orect in parts.items():
        if oref == ref or side[oref] not in (mside, "both"):
            continue
        if not rects_overlap(ring, orect):
            continue
        area = overlap_area_mm2(ring, orect)
        kind = "screw head" if oref in holes else "part"
        (accepted if oref in accepted_refs else blockers).append((oref, kind, area))
    stale = [r_ for r_ in accepted_refs if r_ not in {a[0] for a in accepted}]
    detail = (f"vertical, mates from the {mside}, "
              f"{pcbnew.ToMM(margin):.1f} mm access ring")
    problems = []
    if blockers:
        problems.append("access ring blocked by " + ", ".join(
            f"{r_} ({k}, {a:.1f} mm2)" for r_, k, a in sorted(blockers, key=lambda t: -t[2])[:4]))
    if stale:
        problems.append("accept_blockers names " + ", ".join(stale) + " but nothing of "
                        "that ref is in the ring -- a stale acceptance; drop it")
    if accepted:
        detail += "; accepted: " + ", ".join(f"{r_} ({k}, {a:.1f} mm2)" for r_, k, a in accepted)
    if problems:
        rep.bad(name, detail + "; " + "; ".join(problems))
    else:
        rep.ok(name, detail + ", clear")
    return {
        "ref": ref,
        "mating": "vertical",
        "mating_side": mside,
        "blockers": [{"ref": r_, "kind": k, "area_mm2": round(a, 2)} for r_, k, a in blockers],
        "accepted": [{"ref": r_, "kind": k, "area_mm2": round(a, 2)} for r_, k, a in accepted],
        "ok": not problems,
    }


def check_undeclared(b, cfg: dict, rep: Report) -> None:
    """Every connector-looking part must be declared or explicitly excused.

    A connector missing from [[connectors]] is not checked at all, and no
    check running silently looks like a check passing. So any ref with a
    connector prefix (``[connector_policy] prefixes``) must be either in
    [[connectors]] or in ``not_user_mated`` with a reason.
    """
    pol = cfg.get("connector_policy") or {}
    prefixes = [str(p) for p in (pol.get("prefixes") or DEFAULT_PREFIXES)]
    excused: dict[str, str] = {}
    for e in pol.get("not_user_mated") or []:
        if not str(e.get("reason") or "").strip():
            _lib.fail(f"[connector_policy] not_user_mated {e.get('ref')!r} has no reason")
        excused[str(e.get("ref"))] = str(e["reason"])
    declared = {str(c.get("ref")) for c in (cfg.get("connectors") or [])}
    pat = re.compile(r"^(" + "|".join(re.escape(p) for p in prefixes) + r")\d+$")
    looks = sorted((fp.GetReference() for fp in b.GetFootprints()
                    if pat.match(fp.GetReference())),
                   key=lambda r: (re.sub(r"\d", "", r), int(re.sub(r"\D", "", r) or 0)))
    missing = [r for r in looks if r not in declared and r not in excused]
    stale = [r for r in excused if r not in looks]
    if missing or stale:
        msg = []
        if missing:
            msg.append(f"{', '.join(missing)} look like connectors (prefix "
                       f"{'/'.join(prefixes)}) but are in neither [[connectors]] nor "
                       f"[connector_policy] not_user_mated -- nothing checks a plug fits")
        if stale:
            msg.append(f"not_user_mated names {', '.join(stale)}, not on the board")
        rep.bad("connectors/declared", "; ".join(msg))
    else:
        rep.ok("connectors/declared",
               f"{len(looks)} connector ref(s), all declared"
               + (f" ({len(excused)} excused as not user-mated)" if excused else ""))


# --------------------------------------------------------------------------
# copper in keepouts
# --------------------------------------------------------------------------


def keepout_rects(b, cfg: dict) -> list[tuple[dict, tuple[int, int, int, int]]]:
    """Each declared keepout with its strict-interior rect in KiCad units."""
    FM = pcbnew.FromMM
    frame = _lib.board_frame(b)
    out = []
    for i, ko in enumerate(cfg.get("keepouts") or []):
        missing = [k for k in ("x1", "y1", "x2", "y2") if k not in ko]
        if missing:
            _lib.fail(f"[[keepouts]] #{i + 1} is missing " + ", ".join(missing))
        x1, x2 = sorted((float(ko["x1"]), float(ko["x2"])))
        y1, y2 = sorted((float(ko["y1"]), float(ko["y2"])))
        # The Y flip inverts the y-order, so re-sort -- same trap add_keepouts
        # documents: a rectangle built from (top, bottom) the wrong way round
        # is not a rectangle and finds nothing.
        kx1, ky_a = _lib.to_kicad_xy(frame, x1 + EPS_MM, y1 + EPS_MM)
        kx2, ky_b = _lib.to_kicad_xy(frame, x2 - EPS_MM, y2 - EPS_MM)
        ky1, ky2 = sorted((ky_a, ky_b))
        out.append((ko, (FM(min(kx1, kx2)), FM(ky1), FM(max(kx1, kx2)), FM(ky2))))
    return out


def resolve_layers(b, ko: dict, where: str) -> list[int]:
    names = ko.get("layers")
    if not names:
        return list(b.GetEnabledLayers().CuStack())
    out = []
    for raw in names:
        n = str(raw).replace("_Cu", ".Cu").replace("_", ".")
        lid = b.GetLayerID(n)
        if lid < 0:
            _lib.fail(f"{where}: layer '{raw}' is not on this board")
        out.append(lid)
    return out


def check_keepout_copper(b, cfg: dict, rep: Report) -> list[dict]:
    if not (cfg.get("keepouts") or []):
        rep.skip("keepout-copper", "no [[keepouts]] declared in config")
        return []

    tracks = list(b.GetTracks())
    footprints = list(b.GetFootprints())
    zones = [z for z in b.Zones() if not is_rule_area(z)]

    out = []
    for ko, rect in keepout_rects(b, cfg):
        name = str(ko.get("name", "keepout"))
        where = f"[[keepouts]] '{name}'"
        lids = resolve_layers(b, ko, where)
        lset = set(lids)
        allow_tracks = bool(ko.get("allow_tracks", False))
        allow_vias = bool(ko.get("allow_vias", False))
        allow_pours = bool(ko.get("allow_pours", False))
        allow_pads = bool(ko.get("allow_pads", False))

        hits: list[str] = []

        for t in tracks:
            if t.GetClass() == "PCB_VIA":
                if allow_vias or not any(t.IsOnLayer(lid) for lid in lids):
                    continue
                p = t.GetPosition()
                # A via is a barrel, not a point: it is in the keepout if its
                # annulus reaches in, on any layer the keepout covers.
                r = via_radius(t, lids)
                if rects_overlap(rect, (p.x - r, p.y - r, p.x + r, p.y + r)):
                    hits.append(
                        f"via {t.GetNetname()} at "
                        f"{pcbnew.ToMM(p.x):.2f},{pcbnew.ToMM(p.y):.2f}"
                    )
            else:
                if allow_tracks or t.GetLayer() not in lset:
                    continue
                s, e = t.GetStart(), t.GetEnd()
                if seg_hits_rect(s.x, s.y, e.x, e.y, rect):
                    hits.append(
                        f"track {t.GetNetname()} on {b.GetLayerName(t.GetLayer())} "
                        f"at {pcbnew.ToMM(s.x):.2f},{pcbnew.ToMM(s.y):.2f}"
                    )

        if not allow_pads:
            for fp in footprints:
                for pad in fp.Pads():
                    if not any(pad.IsOnLayer(lid) for lid in lids):
                        continue
                    if rects_overlap(rect, rect_of(pad.GetBoundingBox())):
                        hits.append(
                            f"pad {fp.GetReference()}.{pad.GetNumber()} "
                            f"({pad.GetNetname()})"
                        )

        if not allow_pours:
            for z in zones:
                for lid in lids:
                    if not z.IsOnLayer(lid):
                        continue
                    inside = False
                    try:
                        polys = z.GetFilledPolysList(lid)
                        for oi in range(polys.OutlineCount()):
                            chain = polys.Outline(oi)
                            for pi in range(chain.PointCount()):
                                pt = chain.CPoint(pi)
                                if rect[0] <= pt.x <= rect[2] and rect[1] <= pt.y <= rect[3]:
                                    inside = True
                                    break
                            if inside:
                                break
                    except Exception:  # noqa: BLE001 - unfilled zone / SWIG variance
                        inside = False
                    if inside:
                        hits.append(
                            f"zone {z.GetNetname() or z.GetZoneName()} filled on "
                            f"{b.GetLayerName(lid)}"
                        )

        layer_names = "+".join(b.GetLayerName(lid) for lid in lids)
        if hits:
            shown = "; ".join(hits[:6])
            more = f" (+{len(hits) - 6} more)" if len(hits) > 6 else ""
            rep.bad(
                f"keepout-copper/{name}", f"{len(hits)} item(s) inside: {shown}{more}"
            )
        else:
            rep.ok(f"keepout-copper/{name}", f"clear on {layer_names}")
        out.append({"name": name, "layers": layer_names, "violations": hits})
    return out


# --------------------------------------------------------------------------


def main() -> int:
    ap = _lib.pass_parser(PASS)
    ap.add_argument(
        "--warn-only",
        action="store_true",
        help="report failures but exit 0 (for running mid-build, before the "
        "board is final)",
    )
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no such board")
    cfg = _lib.load_config(args.config)

    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(
            f"{board}: pcbnew returned None. That is what a board written by a "
            "NEWER KiCad looks like to an older pcbnew -- check the majors match."
        )

    rep = Report()
    connectors = check_connectors(b, cfg, rep, board)
    keepouts = check_keepout_copper(b, cfg, rep)

    passed = sum(1 for r in rep.rows if r["status"] == "pass")
    skipped = sum(1 for r in rep.rows if r["status"] == "skip")
    print(
        f"{passed} passed, {rep.failures} failed, {skipped} not checked",
        file=sys.stderr,
    )

    _lib.emit(
        PASS,
        board=str(board),
        passed=passed,
        failed=rep.failures,
        skipped=skipped,
        connectors=connectors,
        keepouts=keepouts,
        rows=rep.rows,
    )
    if rep.failures and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
