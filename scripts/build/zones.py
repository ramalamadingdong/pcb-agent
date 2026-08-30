#!/usr/bin/env python3
"""Copper pours, then the fill.

Adds one copper pour per ``[[zones.pour]]`` entry in board.toml and then
fills every zone on the board with the kct filler.

board.toml
----------
::

    [[zones.pour]]
    net              = "GND"        # required
    layer            = "In1.Cu"     # required
    clearance_mm     = 0.25         # required
    min_thickness_mm = 0.15         # required
    # optional:
    priority          = 0           # higher fills later
    thermal_gap_mm    = 0.3
    thermal_bridge_mm = 0.4
    bbox              = [10.0, 10.0, 40.0, 30.0]   # island pour, absolute mm

Layer names accept either the KiCad spelling (``In1.Cu``) or the gerber
spelling the checker uses (``In1_Cu``); both normalise to ``In1.Cu``.

Order
-----
Pours go down BEFORE the fanout, the fiducials and the keepouts, because
each of those has something the pour must honour — the fanout's vias, the
fiducial pad's clearance ring, the keepout rule areas.  Anything that adds
copper after a pour leaves that pour stale, so run this pass again with
``--fill-only`` once the copper-adding passes have finished.  That is the
same two-step the hand-written build did: pour early, refill last.

Idempotence
-----------
Strips every non-rule-area zone before adding, so a re-run rebuilds rather
than stacking a second pour on every layer.  Rule areas (the keepouts
``add_keepouts.py`` writes) are left alone — they are zones too, and
deleting them here would silently un-ban the antenna band.

Not ported
----------
``stitch_planes.py`` from the source pipeline is **not** cross-layer plane
stitching despite the name: it drops one escape via + stub next to four
named resistor pads that the router had left stranded, using the same
geometry as ``fanout.py`` with a 32-point radial search instead of an edge
normal.  It is a hand-list of four refdes on one board with no generic
rule behind it, so there is nothing to port — declare those pads as
``[[fanout.thermal_vias]]`` entries if you need the same effect.

Usage:
    python3 zones.py --board board.kicad_pcb --config board.toml
    python3 zones.py --board board.kicad_pcb --fill-only
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pcbnew

import _lib

PASS = "zones"

# The kct CLI. Override with KCT=... when it is behind a launcher, e.g.
# KCT="uv run --project /opt/kicad-tools kct".
KCT = os.environ.get("KCT", "kct")


def layer_name(name: str) -> str:
    """Accept the gerber spelling the checker uses (``In1_Cu``) as well as
    KiCad's own (``In1.Cu``).  board.toml is read by both."""
    return name.replace("_Cu", ".Cu").replace("_", ".")


def run_kct(args: list[str]) -> str:
    cmd = shlex.split(KCT, posix=(os.name != "nt")) + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        print(f"    kct: {line}", file=sys.stderr)
    if proc.returncode != 0:
        _lib.fail(
            f"{' '.join(cmd)} exited {proc.returncode} — "
            "is kct on PATH? override with the KCT environment variable"
        )
    return out


def strip_pours(board_path: Path) -> int:
    """Remove every copper pour, keeping rule areas.

    Rule areas are zones as far as pcbnew is concerned, and they belong to
    add_keepouts.py.  Removing them here would quietly delete the antenna
    band and every plane no-routing area on the next zones run.
    """
    b = pcbnew.LoadBoard(str(board_path))
    doomed = [z for z in b.Zones() if not z.GetIsRuleArea()]
    if not doomed:
        return 0
    for z in doomed:
        b.Remove(z)
    pcbnew.SaveBoard(str(board_path), b)
    return len(doomed)


def main() -> int:
    ap = _lib.pass_parser(PASS)
    ap.add_argument(
        "--fill-only",
        action="store_true",
        help="skip the pour add/strip, just refill (run this after fanout/silk)",
    )
    ap.add_argument(
        "--no-fill",
        action="store_true",
        help="add the pours but leave them unfilled",
    )
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no such board")

    cfg = _lib.load_config(args.config)
    pours = cfg.get("zones", {}).get("pour", [])

    stripped = 0
    added: list[dict] = []

    if not args.fill_only:
        if not pours:
            _lib.fail(
                f"{args.config}: no [[zones.pour]] entries — "
                "an unpoured 4-layer board is almost never what you meant"
            )

        stripped = strip_pours(board)
        if stripped:
            print(f"  stripped {stripped} existing pour(s)", file=sys.stderr)
            _lib.assert_net_table(board)

        bbox_frame = None  # lazily read once, only when a pour declares a bbox
        for i, p in enumerate(pours):
            missing = [
                k
                for k in ("net", "layer", "clearance_mm", "min_thickness_mm")
                if k not in p
            ]
            if missing:
                _lib.fail(
                    f"{args.config}: [[zones.pour]] #{i + 1} is missing "
                    + ", ".join(missing)
                )
            net = str(p["net"])
            layer = layer_name(str(p["layer"]))
            cmd = [
                "zones",
                "add",
                str(board),
                "-o",
                str(board),
                "--net",
                net,
                "--layer",
                layer,
                "--clearance",
                str(float(p["clearance_mm"])),
                "--min-thickness",
                str(float(p["min_thickness_mm"])),
            ]
            if "priority" in p:
                cmd += ["--priority", str(int(p["priority"]))]
            if "thermal_gap_mm" in p:
                cmd += ["--thermal-gap", str(float(p["thermal_gap_mm"]))]
            if "thermal_bridge_mm" in p:
                cmd += ["--thermal-bridge", str(float(p["thermal_bridge_mm"]))]
            if "bbox" in p:
                bb = [float(v) for v in p["bbox"]]
                if len(bb) != 4:
                    _lib.fail(
                        f"{args.config}: [[zones.pour]] #{i + 1} bbox needs "
                        "4 values [minx, miny, maxx, maxy]"
                    )
                # config is board-frame (bottom-left, Y-up) — see
                # _lib.board_frame; kct --bbox is sheet-absolute Y-down.
                if bbox_frame is None:
                    fb = pcbnew.LoadBoard(str(board))
                    if fb is None:
                        _lib.fail(f"pcbnew could not load {board} for the frame")
                    bbox_frame = _lib.board_frame(fb)
                kx1, ky1 = _lib.to_kicad_xy(bbox_frame, bb[0], bb[1])
                kx2, ky2 = _lib.to_kicad_xy(bbox_frame, bb[2], bb[3])
                bb = [min(kx1, kx2), min(ky1, ky2), max(kx1, kx2), max(ky1, ky2)]
                cmd += ["--bbox", ",".join(f"{v:.3f}" for v in bb)]

            print(f"  pour: {net} on {layer}", file=sys.stderr)
            run_kct(cmd)
            # A pour add rewrites the board.  Check the net table every time:
            # a pour on a net the board does not carry is a config typo, and
            # a stripped net table makes the router a silent no-op later.
            _lib.assert_net_table(board)
            added.append({"net": net, "layer": layer})

    filled = False
    if not args.no_fill:
        # Fill with the kct filler rather than leaning on
        # `kicad-cli pcb drc --refill-zones`: that refill starves connector
        # GND thermals to one spoke and invents phantom `starved_thermal`
        # errors, so DRC then reports faults the board does not have.
        # Never add --save-board to a kicad-cli call here either — it wipes
        # the net table, and the router afterwards reports "nets to route: 0"
        # and does nothing, silently.
        run_kct(["zones", "fill", str(board), "-o", str(board)])
        _lib.assert_net_table(board)
        filled = True

    b = pcbnew.LoadBoard(str(board))
    zones = list(b.Zones())
    pour_zones = [z for z in zones if not z.GetIsRuleArea()]
    unfilled = [z for z in pour_zones if not z.IsFilled()]
    for z in pour_zones:
        print(
            f"  {z.GetNetname() or '<no net>':<8} "
            f"{b.GetLayerName(z.GetLayer()):<8} "
            f"filled={z.IsFilled()} area={z.GetFilledArea() / 1e12:.1f} mm2",
            file=sys.stderr,
        )
    if filled and unfilled:
        _lib.fail(
            f"{len(unfilled)} zone(s) did not fill — an unfilled pour is not "
            "copper, and every downstream check will read the board as if it were"
        )

    _lib.emit(
        PASS,
        board=str(board),
        stripped=stripped,
        pours_added=added,
        pour_zones=len(pour_zones),
        rule_areas=len(zones) - len(pour_zones),
        filled=filled,
        filled_area_mm2=round(sum(z.GetFilledArea() for z in pour_zones) / 1e12, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
