#!/usr/bin/env python3
"""Add keepout rule areas from board.toml ``[[keepouts]]``.

A keepout that exists only as a comment in the floorplan is not a keepout.
The board this was ported from carried a "top 8 mm keepout" for a module's
PCB antenna in prose only, and all four pours flooded straight through it.
This pass turns every declared rectangle into a real KiCad rule area, so DRC
enforces it and ``scripts/validate_gerbers.py`` can confirm it in the gerbers.

board.toml — the SAME schema the checker reads
----------------------------------------------
::

    [[keepouts]]
    name   = "antenna"
    x1     = 50.0          # board-frame mm: origin at the board's BOTTOM-LEFT
    y1     = 30.0          # corner, X right, Y UP — the same frame [frame]
    x2     = 60.0          # outline_bbox and validate_gerbers.py use.  KiCad
    y2     = 40.0          # pages run Y DOWN; see _lib.board_frame.
    layers = ["F_Cu", "In1_Cu", "In2_Cu", "B_Cu"]
    # optional, all default false — a keepout bans pours, tracks and vias:
    allow_pours  = false
    allow_tracks = false
    allow_vias   = false

Layer names accept the gerber spelling the checker uses (``In1_Cu``) or
KiCad's own (``In1.Cu``).  Omitting ``layers`` bans on every copper layer.

Two recipes worth knowing:

* **RF antenna clearance** — everything banned on every copper layer.  The
  defaults, no flags.
* **A plane layer that must stay solid** — a full-board rectangle with
  ``allow_pours = true`` and ``allow_vias = true``, banning only tracks.
  A layer declared a solid reference plane in the plan is not one until
  something enforces it: one board shipped with 60 router track segments
  across 17 signal nets on its RF reference plane, and the next revision
  shipped 474 mm of signal track on the power plane one layer down — the
  router simply used whichever inner layer was not yet banned as its escape
  layer.  The DSN export marking those layers ``(type power)`` stops the
  router trying; this rule area is the DRC backstop that catches any other
  tool doing it.

Note keepouts do NOT ban pads.  A fiducial or a mounting pad inside one is
placed without complaint — ``add_fiducials.py`` and the checker both test for
that separately.

Idempotent: every rule area this pass creates is named ``KEEPOUT_<name>``,
and every zone with that prefix is removed before adding.  A keepout deleted
from board.toml therefore disappears from the board on the next run instead
of lingering as stale geometry.  (Geometry is reproduced exactly; the KIIDs
pcbnew mints for new items are not settable from Python, so the file is not
byte-identical at the UUID level.)

Optional: pad-clearance rings
-----------------------------
``[keepouts_from_pad_clearance] enabled = true`` additionally mirrors every
footprint-local pad clearance override into a matching rule area on the
routable layers.  Specctra DSN cannot express a footprint-local clearance, so
the router never sees it — a power net got routed straight into a mounting
hole's clearance ring on one board because of exactly this.  Requires the DSN
export to inject matching keepouts; these are the board-side/DRC view of the
same rule.

Usage:
    python3 add_keepouts.py --board board.kicad_pcb --config board.toml
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pcbnew

import _lib

PASS = "add_keepouts"

NAME_PREFIX = "KEEPOUT_"
DEFAULT_COPPER = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
RING_SEGMENTS = 16


def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses (``In1_Cu``) as well as
    KiCad's own (``In1.Cu``).  board.toml is read by both."""
    return name.replace("_Cu", ".Cu").replace("_", ".")


def layer_set(b, names: list[str], where: str):
    ls = pcbnew.LSET()
    resolved = []
    for raw in names:
        n = layer_name(str(raw))
        lid = b.GetLayerID(n)
        if lid < 0:
            _lib.fail(
                f"{where}: layer '{raw}' is not on this board. "
                "Don't assume the generated layer names match yours — "
                f"available copper: {', '.join(copper_layers(b))}"
            )
        ls.AddLayer(lid)
        resolved.append(n)
    return ls, resolved


def copper_layers(b) -> list[str]:
    out = []
    for n in DEFAULT_COPPER:
        if b.GetLayerID(n) >= 0:
            out.append(n)
    return out


def main() -> int:
    args = _lib.pass_parser(PASS).parse_args()
    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no such board")

    cfg = _lib.load_config(args.config)
    keepouts = cfg.get("keepouts", []) or []
    ring_cfg = cfg.get("keepouts_from_pad_clearance", {}) or {}
    want_rings = bool(ring_cfg.get("enabled", False))

    if not keepouts and not want_rings:
        _lib.fail(
            f"{args.config}: no [[keepouts]] declared. If this board genuinely "
            "has no RF or analog clearance to protect, drop this pass from the "
            "Makefile rather than running it empty — a keepout nobody declared "
            "is indistinguishable from a keepout nobody enforced."
        )

    b = pcbnew.LoadBoard(str(board))
    FM = pcbnew.FromMM
    # config is board-frame (bottom-left, Y-up) — see _lib.board_frame
    frame = _lib.board_frame(b)

    # Collect clearance-override pads NOW, before ANY zone Add/Remove --
    # pcbnew's SWIG wrapping goes stale after container mutation and
    # GetFootprints() starts returning raw pointers (iterating pads after
    # the zone strip below segfaults hard).
    ring_pads = []
    if want_rings:
        for fp in b.GetFootprints():
            for p in fp.Pads():
                try:
                    lc = p.GetLocalClearance()
                except TypeError:
                    lc = p.GetLocalClearance(None)
                if not lc:
                    continue
                pos = p.GetPosition()
                r = max(p.GetSize().x, p.GetSize().y) // 2 + int(lc)
                ring_pads.append((pos.x, pos.y, r))

    # strip previous runs
    removed = 0
    for z in list(b.Zones()):
        name = z.GetZoneName() or ""
        if name.startswith(NAME_PREFIX):
            b.Remove(z)
            removed += 1
    if removed:
        print(f"  removed {removed} existing keepout rule area(s)", file=sys.stderr)

    added = []

    def add_rule_area(name, x0, y0, x1, y1, layers, no_tracks, no_vias, no_pour=True):
        z = pcbnew.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetZoneName(name)
        z.SetLayerSet(layers)
        z.SetDoNotAllowZoneFills(no_pour)
        z.SetDoNotAllowTracks(no_tracks)
        z.SetDoNotAllowVias(no_vias)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        chain = pcbnew.SHAPE_LINE_CHAIN()
        for x, y in pts:
            chain.Append(int(x), int(y))
        chain.SetClosed(True)
        z.AddPolygon(chain)
        b.Add(z)

    for i, ko in enumerate(keepouts):
        where = f"[[keepouts]] #{i + 1}"
        missing = [k for k in ("x1", "y1", "x2", "y2") if k not in ko]
        if missing:
            _lib.fail(f"{where} is missing " + ", ".join(missing))
        raw_name = str(ko.get("name", f"keepout{i + 1}"))
        zname = NAME_PREFIX + raw_name
        x1, x2 = sorted((float(ko["x1"]), float(ko["x2"])))
        y1, y2 = sorted((float(ko["y1"]), float(ko["y2"])))
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            _lib.fail(f"{where} '{raw_name}' is a zero-area rectangle")

        # config is board-frame (bottom-left, Y-up) — see _lib.board_frame.
        # The Y flip inverts the y-order, so re-sort: a rule area built from
        # (top, bottom) the wrong way round is not a well-formed rectangle.
        kx1, ky_a = _lib.to_kicad_xy(frame, x1, y1)
        kx2, ky_b = _lib.to_kicad_xy(frame, x2, y2)
        ky1, ky2 = sorted((ky_a, ky_b))

        names = ko.get("layers") or list(copper_layers(b))
        lset, resolved = layer_set(b, list(names), f"{where} '{raw_name}'")

        no_pour = not bool(ko.get("allow_pours", False))
        no_tracks = not bool(ko.get("allow_tracks", False))
        no_vias = not bool(ko.get("allow_vias", False))

        add_rule_area(
            zname, FM(kx1), FM(ky1), FM(kx2), FM(ky2), lset, no_tracks, no_vias, no_pour
        )
        print(
            f"  {zname}: ({x1:.1f},{y1:.1f})...({x2:.1f},{y2:.1f}) board-frame, "
            f"layers={'+'.join(resolved)}, "
            f"pour={'NO' if no_pour else 'yes'} "
            f"tracks={'NO' if no_tracks else 'yes'} "
            f"vias={'NO' if no_vias else 'yes'}",
            file=sys.stderr,
        )
        added.append(
            {
                "name": zname,
                "rect": [x1, y1, x2, y2],
                "layers": resolved,
                "bans": {
                    "pours": no_pour,
                    "tracks": no_tracks,
                    "vias": no_vias,
                },
            }
        )

    rings = 0
    if want_rings and ring_pads:
        names = ring_cfg.get("layers") or ["F.Cu", "B.Cu"]
        rlset, rresolved = layer_set(
            b, list(names), "[keepouts_from_pad_clearance]"
        )
        # 16-gon circle, NOT a bounding square -- the pad's clearance override
        # is radial, and a square rule area banned legal copper in its corners
        # (0.45 mm2 per corner; a hand-routed USB pair tripped it).  The
        # injected DSN keepouts are circles, so this keeps board and DSN
        # agreeing.
        for px, py, r in ring_pads:
            z = pcbnew.ZONE(b)
            z.SetIsRuleArea(True)
            z.SetZoneName(NAME_PREFIX + "PAD_RING")
            z.SetLayerSet(rlset)
            z.SetDoNotAllowZoneFills(False)
            z.SetDoNotAllowTracks(True)
            z.SetDoNotAllowVias(True)
            z.SetDoNotAllowPads(False)
            z.SetDoNotAllowFootprints(False)
            chain = pcbnew.SHAPE_LINE_CHAIN()
            for q in range(RING_SEGMENTS):
                a = 2 * math.pi * q / RING_SEGMENTS
                chain.Append(int(px + r * math.cos(a)), int(py + r * math.sin(a)))
            chain.SetClosed(True)
            z.AddPolygon(chain)
            b.Add(z)
            rings += 1
        print(
            f"  pad-clearance rule areas: {rings} ({RING_SEGMENTS}-gon) on "
            f"{'+'.join(rresolved)}",
            file=sys.stderr,
        )

    pcbnew.SaveBoard(str(board), b)
    _lib.assert_net_table(board)
    print("saved", file=sys.stderr)

    _lib.emit(
        PASS,
        board=str(board),
        removed=removed,
        keepouts=added,
        pad_clearance_rings=rings,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
