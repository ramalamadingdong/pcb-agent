#!/usr/bin/env python3
"""Add assembly fiducials.

Pick-and-place wants >= 3 fiducials per assembled side to register the board
before placing fine-pitch parts.  On a board with 0.3 mm pads the
registration actually matters.

Fiducials are *not* netlist objects -- `create_pcb` will never emit them from
the schematic -- so they have to be injected as a separate pass, the same way
`fanout.py` injects copper.  Run this after the board exists and BEFORE the
pours are filled: the fiducial pad carries a clearance ring that the pour has
to honour, or the flood swallows the mark and the pick-and-place camera has
nothing to lock onto.  (`zones.py --fill-only` is the refill that comes after.)

THE FABRICATION ATTRIBUTE
-------------------------
Each fiducial pad is stamped ``PAD_PROP_FIDUCIAL_GLBL``, which is what KiCad
exports as the Gerber X2 attribute ``.AperFunction,FiducialPad,Global``.  That
tag is the ONLY thing that distinguishes a fiducial from any other round
copper dot once the board is gerbers, and it is what
``scripts/validate_gerbers.py`` counts:

    if f.aperture.function and "fiducial" in f.aperture.function.lower():

**The source pipeline this pass was ported from did NOT set it.**  Its
vendored ``Fiducial_1mm_Mask2mm.kicad_mod`` declares the pad as a plain
``(pad "" smd circle ...)`` with a solder-mask margin and a clearance
override and no ``(property pad_prop_fiducial_glob)`` at all — so that board
shipped fiducials that were invisible to every gerber-level check, and the
count check would have reported "none found" on a board that physically had
three.  The attribute is added here, and this pass hard-fails if the running
pcbnew does not expose the constant rather than writing an untagged pad.

Export with X2 attributes ON or the tag never reaches the gerbers.

ASSEMBLY DATA
-------------
Fiducials are copper features, not placed components.  An orphan designator
(present in the CPL, absent from the BOM) makes a fab reject an entire PCBA
order, so by default the footprint is marked both `exclude_from_bom` and
`exclude_from_pos_files` and never reaches either file.  Set
``exclude_from_pos = false`` if your fab wants them in the position file and
you have a downstream filter that drops them from the CPL.

board.toml
----------
::

    [fiducials]
    count = 3
    positions = [[5.0, 25.0], [50.0, 46.0], [8.5, 66.5]]   # board-frame mm
    min_collinear_deviation_mm = 1.0    # also read by validate_gerbers.py
    # optional:
    copper_dia_mm = 1.0
    mask_dia_mm   = 2.0
    clearance_mm  = 0.6      # the ring the pour must honour
    ref_prefix    = "FID"
    exclude_from_pos = true

Coordinates are BOARD-FRAME millimetres: origin at the board's bottom-left
corner, X right, Y UP — the same frame every other rectangle in board.toml
uses, so ``[[keepouts]]`` and ``[frame]`` mean the same thing here as they do
to the checker.  KiCad pages run Y DOWN, so these are converted on the way
into pcbnew; see ``_lib.board_frame``.

Idempotent: strips any fiducial it previously placed before adding, so
re-running is safe.  (Geometry is reproduced exactly; the KIIDs pcbnew mints
for new items are not settable from Python, so the file is not byte-identical
at the UUID level.)

Usage:
    python3 add_fiducials.py --board board.kicad_pcb --config board.toml
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pcbnew

import _lib

PASS = "add_fiducials"

FP_NAME = "Fiducial_pcb-agent"


def fiducial_pad_property():
    """The X2 AperFunction=FiducialPad tag, or a hard failure.

    Global (board-level) rather than Local (panel-level): these register the
    board itself.  If a pcbnew build ever drops the constant, stop — an
    untagged fiducial is a fiducial no gerber-level check can see, which is
    exactly the defect this pass exists to prevent.
    """
    prop = getattr(pcbnew, "PAD_PROP_FIDUCIAL_GLBL", None)
    if prop is None:
        prop = getattr(pcbnew, "PAD_PROP_FIDUCIAL_GLOBAL", None)
    if prop is None:
        _lib.fail(
            "this pcbnew exposes no PAD_PROP_FIDUCIAL_GLBL — refusing to write "
            "fiducials without the X2 AperFunction=FiducialPad attribute, "
            "because nothing downstream could tell them from ordinary copper"
        )
    return prop


def strip_existing(b) -> int:
    """Remove previously-placed fiducial footprints (makes this idempotent).

    Collect the doomed list BEFORE removing anything: pcbnew's SWIG wrappers
    go stale once a container is mutated, and iterating GetFootprints() while
    removing from it hands back raw pointers.
    """
    doomed = [
        fp
        for fp in b.GetFootprints()
        if fp.GetFPIDAsString().endswith(FP_NAME) or fp.GetValue() == FP_NAME
    ]
    for fp in doomed:
        b.Remove(fp)
    return len(doomed)


def in_rect(x: float, y: float, ko: dict) -> bool:
    x1, x2 = sorted((float(ko["x1"]), float(ko["x2"])))
    y1, y2 = sorted((float(ko["y1"]), float(ko["y2"])))
    return x1 <= x <= x2 and y1 <= y <= y2


def worst_collinear_deviation(pts: list[tuple[float, float]]) -> float:
    """Largest perpendicular offset of any triple from the line of its other
    two — the same statistic validate_gerbers.py computes.  Zero means the
    vision system cannot resolve rotation, only translation."""
    worst = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            for k in range(j + 1, len(pts)):
                a, b, c = pts[i], pts[j], pts[k]
                area2 = abs(
                    (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
                )
                base = math.hypot(b[0] - a[0], b[1] - a[1])
                worst = max(worst, area2 / base if base else 0.0)
    return worst


def build_fiducial(b, ref: str, x_mm: float, y_mm: float, geo: dict):
    """x_mm/y_mm are KiCad page mm — the caller has already converted them out
    of board.toml's board frame (see ``_lib.board_frame``)."""
    FM = pcbnew.FromMM
    fp = pcbnew.FOOTPRINT(b)
    try:
        fp.SetFPID(pcbnew.LIB_ID("", FP_NAME))
    except Exception:  # pragma: no cover - LIB_ID ctor differs across majors
        lid = pcbnew.LIB_ID()
        lid.Parse(FP_NAME)
        fp.SetFPID(lid)
    fp.SetReference(ref)
    fp.SetValue(FP_NAME)
    fp.SetPosition(pcbnew.VECTOR2I(FM(x_mm), FM(y_mm)))

    attrs = pcbnew.FP_SMD | pcbnew.FP_EXCLUDE_FROM_BOM
    if geo["exclude_from_pos"]:
        attrs |= pcbnew.FP_EXCLUDE_FROM_POS_FILES
    fp.SetAttributes(attrs)

    pad = pcbnew.PAD(fp)
    pad.SetNumber("")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(FM(geo["copper_dia"]), FM(geo["copper_dia"])))
    pad.SetPosition(fp.GetPosition())
    lset = pcbnew.LSET()
    lset.AddLayer(b.GetLayerID("F.Cu"))
    lset.AddLayer(b.GetLayerID("F.Mask"))
    pad.SetLayerSet(lset)
    # Mask opening larger than the copper (the "Level A" fiducial): the
    # camera needs bare metal with a clean surround, not a dot flooded by
    # soldermask.
    pad.SetLocalSolderMaskMargin(
        FM((geo["mask_dia"] - geo["copper_dia"]) / 2.0)
    )
    # The clearance ring is what keeps the pour off the mark.  Without it the
    # GND flood closes right up to the copper dot and the pick-and-place
    # camera has nothing to lock onto — which is why this pass has to run
    # before the pour is filled, not after.
    pad.SetLocalClearance(FM(geo["clearance"]))
    pad.SetProperty(fiducial_pad_property())
    fp.Add(pad)

    # A courtyard so the placement passes treat the fiducial as an obstacle
    # rather than packing a part on top of it.
    crt = pcbnew.PCB_SHAPE(fp)
    crt.SetShape(pcbnew.SHAPE_T_CIRCLE)
    crt.SetCenter(fp.GetPosition())
    crt.SetEnd(
        pcbnew.VECTOR2I(
            FM(x_mm + geo["mask_dia"] / 2.0 + 0.25), FM(y_mm)
        )
    )
    crt.SetLayer(b.GetLayerID("F.CrtYd"))
    crt.SetWidth(FM(0.05))
    fp.Add(crt)

    b.Add(fp)
    return fp


def main() -> int:
    args = _lib.pass_parser(PASS).parse_args()
    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no such board")

    cfg = _lib.load_config(args.config)
    fd = cfg.get("fiducials", {}) or {}
    positions = fd.get("positions")
    if not positions:
        _lib.fail(
            f"{args.config}: [fiducials] has no positions — add "
            "positions = [[x, y], ...] in absolute board mm. Nothing else "
            "emits fiducials, so an unset list means a board without any."
        )
    want = fd.get("count")
    if want is not None and int(want) != len(positions):
        _lib.fail(
            f"{args.config}: [fiducials] count = {want} but "
            f"{len(positions)} positions — the checker counts what is on the "
            "board, so these must agree"
        )
    pts = []
    for i, p in enumerate(positions):
        if len(p) != 2:
            _lib.fail(f"{args.config}: [fiducials] positions[{i}] is not [x, y]")
        pts.append((float(p[0]), float(p[1])))

    geo = {
        "copper_dia": float(fd.get("copper_dia_mm", 1.0)),
        "mask_dia": float(fd.get("mask_dia_mm", 2.0)),
        "clearance": float(fd.get("clearance_mm", 0.6)),
        "exclude_from_pos": bool(fd.get("exclude_from_pos", True)),
    }
    prefix = str(fd.get("ref_prefix", "FID"))

    # Non-collinear or the vision system solves translation but not rotation.
    min_dev = float(fd.get("min_collinear_deviation_mm", 1.0))
    if len(pts) >= 3:
        worst = worst_collinear_deviation(pts)
        if worst < min_dev:
            _lib.fail(
                f"fiducials are collinear: most off-axis triple deviates only "
                f"{worst:.2f} mm (need {min_dev} mm) — assembly can't resolve "
                "rotation. Move one off the line."
            )
    elif len(pts) < 3:
        print(
            f"  WARNING: only {len(pts)} fiducial(s) — assembly wants >= 3 per "
            "assembled side to solve rotation",
            file=sys.stderr,
        )

    # Keepout rules ban pours, tracks and vias — NOT pads — so a fiducial
    # inside one is placed without complaint and then sits under a module
    # antenna as bare copper.  A corner formula did exactly that on the board
    # this was ported from; explicit reviewed positions are the fix, and this
    # is the check that keeps them honest.  Both sides of this test are raw
    # board.toml values, so it runs in the board frame — no conversion here.
    for ko in cfg.get("keepouts", []) or []:
        inside = [p for p in pts if in_rect(p[0], p[1], ko)]
        if inside:
            _lib.fail(
                f"{len(inside)} fiducial(s) inside keepout "
                f"'{ko.get('name', '?')}' — keepout rules ban pours, tracks "
                "and vias, not pads, so nothing else would have stopped this"
            )

    b = pcbnew.LoadBoard(str(board))

    bb = b.GetBoardEdgesBoundingBox()
    if bb.GetWidth() <= 0 or bb.GetHeight() <= 0:
        _lib.fail("no Edge.Cuts outline found -- cannot place fiducials")
    ring = geo["mask_dia"] / 2.0 + geo["clearance"]
    lo_x, lo_y = pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop())
    hi_x, hi_y = pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())

    # config is board-frame (bottom-left, Y-up) — see _lib.board_frame.  This
    # is the single conversion in this pass: everything below (the outline
    # check and the pad placement) works in KiCad page mm.
    frame = _lib.board_frame(b)
    kpts = [_lib.to_kicad_xy(frame, x, y) for x, y in pts]

    for i, ((x, y), (kx, ky)) in enumerate(zip(pts, kpts)):
        if not (lo_x + ring <= kx <= hi_x - ring and lo_y + ring <= ky <= hi_y - ring):
            _lib.fail(
                f"fiducial {i + 1} at board-frame ({x}, {y}) — KiCad "
                f"({kx:.1f}, {ky:.1f}) — is outside the board outline "
                f"({lo_x:.1f},{lo_y:.1f})-({hi_x:.1f},{hi_y:.1f}) once its "
                f"{ring:.2f} mm mask+clearance ring is counted"
            )

    # A fiducial IS a pad, and nothing above checks it against the pads already
    # on the board: the outline test only asks whether it is on the board, and
    # the keepout test only covers declared rectangles. So a fiducial parked on
    # a connector passes every check here and ships as a short.
    #
    # That happened. FID2 at (50, 3) sat on J4 pin 1 with 0.0000 mm between
    # them, which DRC reported three ways -- clearance, front solder-mask
    # bridge, courtyard overlap -- and which export_dsn then compounded by
    # turning the fiducial's clearance override into a router keepout ring over
    # that pad, leaving +5V_ARM1 the one unroutable net on the board. One
    # missing check, four symptoms.
    #
    # Runs BEFORE strip_existing so it measures against real neighbours and not
    # against the fiducials this pass is about to replace.
    is_fid = lambda fp: (fp.GetFPIDAsString().endswith(FP_NAME)
                         or fp.GetValue() == FP_NAME)
    clash = []
    for i, (kx, ky) in enumerate(kpts):
        cx, cy = pcbnew.FromMM(kx), pcbnew.FromMM(ky)
        for fp in b.GetFootprints():
            if is_fid(fp):
                continue
            for p in fp.Pads():
                pb = p.GetBoundingBox()
                dx = max(pb.GetLeft() - cx, cx - pb.GetRight(), 0)
                dy = max(pb.GetTop() - cy, cy - pb.GetBottom(), 0)
                gap = pcbnew.ToMM(int(math.hypot(dx, dy))) - ring
                if gap < 0:
                    clash.append((f"{prefix}{i + 1}", pts[i],
                                  f"{fp.GetReference()} pad {p.GetPadName()}",
                                  gap))
    if clash:
        lines = "\n".join(
            f"    {ref} at board-frame {pos} vs {who}: "
            f"{-gap:.3f} mm short of its {ring:.2f} mm mask+clearance ring"
            for ref, pos, who, gap in sorted(clash, key=lambda c: c[3]))
        _lib.fail(
            f"{len(clash)} fiducial/pad conflict(s) — a fiducial is a pad, and "
            f"one on a component pad is a short that the outline and keepout "
            f"checks above cannot see:\n{lines}\n"
            f"  Move it in [fiducials] positions. Measure the free space rather "
            f"than guessing: the connector ranks and the mounting-hole heads "
            f"take more of the edge than a floorplan sketch suggests."
        )

    removed = strip_existing(b)
    if removed:
        print(f"  stripped {removed} existing fiducial(s)", file=sys.stderr)

    placed = []
    for i, ((x, y), (kx, ky)) in enumerate(zip(pts, kpts)):
        ref = f"{prefix}{i + 1}"
        build_fiducial(b, ref, kx, ky, geo)
        # Reported in the board frame — the frame board.toml declared them in
        # and the frame validate_gerbers.py will look for them in.
        placed.append({"ref": ref, "x": x, "y": y})
        print(
            f"  {ref}: board-frame ({x:.1f}, {y:.1f})  "
            f"[KiCad ({kx:.1f}, {ky:.1f})]",
            file=sys.stderr,
        )

    pcbnew.SaveBoard(str(board), b)
    _lib.assert_net_table(board)

    print(
        f"Placed {len(placed)} fiducials, {geo['copper_dia']} mm copper / "
        f"{geo['mask_dia']} mm mask, {geo['clearance']} mm pour ring, "
        "X2 AperFunction=FiducialPad",
        file=sys.stderr,
    )

    _lib.emit(
        PASS,
        board=str(board),
        stripped=removed,
        fiducials=placed,
        copper_dia_mm=geo["copper_dia"],
        mask_dia_mm=geo["mask_dia"],
        clearance_mm=geo["clearance"],
        aper_function="FiducialPad,Global",
        excluded_from_pos_files=geo["exclude_from_pos"],
        min_collinear_deviation_mm=(
            round(worst_collinear_deviation(pts), 3) if len(pts) >= 3 else None
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
