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

import json
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


def strip_pours(board_path: Path, bbox_pours: list[dict]) -> tuple[int, list[str]]:
    """Remove every copper pour (keeping rule areas), then create the
    bounded (bbox) pours — all in ONE pcbnew session.

    Rule areas are zones as far as pcbnew is concerned, and they belong to
    add_keepouts.py.  Removing them here would quietly delete the antenna
    band and every plane no-routing area on the next zones run.

    The bbox pours happen here rather than in a later step because a
    second pcbnew.LoadBoard in the same process can hand back a bare
    SwigPyObject with no BOARD methods (stale SWIG wrappers — the same
    trap the cleanup pass documents). One load, one save. They go through
    pcbnew at all because the pinned kct's `zones add` advertises --bbox
    in its subparser but its top-level CLI rejects it.
    """
    b = pcbnew.LoadBoard(str(board_path))
    if b is None:
        _lib.fail(f"pcbnew could not load {board_path}")
    doomed = [z for z in b.Zones() if not z.GetIsRuleArea()]
    for z in doomed:
        b.Remove(z)

    created: list[str] = []
    if bbox_pours:
        # config is board-frame (bottom-left, Y-up) — see _lib.board_frame
        frame = _lib.board_frame(b)
        FM = pcbnew.FromMM
        for p in bbox_pours:
            net = str(p["net"])
            layer = layer_name(str(p["layer"]))
            bb = [float(v) for v in p["bbox"]]
            kx1, ky1 = _lib.to_kicad_xy(frame, bb[0], bb[1])
            kx2, ky2 = _lib.to_kicad_xy(frame, bb[2], bb[3])
            x1, x2 = sorted((kx1, kx2))
            y1, y2 = sorted((ky1, ky2))
            netinfo = b.FindNet(net)
            if netinfo is None:
                _lib.fail(f"[[zones.pour]] bbox: net {net!r} is not on the board")
            lid = b.GetLayerID(layer)
            if lid < 0:
                _lib.fail(f"[[zones.pour]] bbox: unknown layer {layer!r}")
            z = pcbnew.ZONE(b)
            z.SetLayer(lid)
            z.SetLocalClearance(FM(float(p["clearance_mm"])))
            z.SetMinThickness(FM(float(p["min_thickness_mm"])))
            if "priority" in p:
                z.SetAssignedPriority(int(p["priority"]))
            z.SetZoneName(f"POUR_{net}_{layer}")
            sps = z.Outline()
            sps.NewOutline()
            for px, py in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
                sps.Append(FM(px), FM(py))
            b.Add(z)
            # Net AFTER Add: adding a zone to the board resets its net to 0
            # (a net-less pour is dead copper the verification below flags).
            z.SetNetCode(netinfo.GetNetCode())
            if z.GetNetname() != net:
                _lib.fail(f"bbox pour net assignment failed: got {z.GetNetname()!r}, wanted {net!r}")
            created.append(f"{net}@{layer}")

    if doomed or created:
        pcbnew.SaveBoard(str(board_path), b)
    return len(doomed), created


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

        # Validate every pour up front, split bounded from full-board.
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
            if "bbox" in p and len([float(v) for v in p["bbox"]]) != 4:
                _lib.fail(
                    f"{args.config}: [[zones.pour]] #{i + 1} bbox needs "
                    "4 values [minx, miny, maxx, maxy]"
                )
        bbox_pours = [p for p in pours if "bbox" in p]
        kct_pours = [p for p in pours if "bbox" not in p]

        stripped, created = strip_pours(board, bbox_pours)
        if stripped:
            print(f"  stripped {stripped} existing pour(s)", file=sys.stderr)
        for name in created:
            print(f"  pour: {name} (bbox, via pcbnew)", file=sys.stderr)
        if stripped or created:
            _lib.assert_net_table(board)
        added += [{"net": p["net"], "layer": layer_name(str(p["layer"]))}
                  for p in bbox_pours]

        for p in kct_pours:
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

    # Verify in a FRESH interpreter: a second pcbnew.LoadBoard in a process
    # that already added/removed zones hands back a bare SwigPyObject with
    # no BOARD methods (stale SWIG wrappers). A subprocess sees clean state.
    verify_src = (
        "import json, sys, pcbnew\n"
        "b = pcbnew.LoadBoard(sys.argv[1])\n"
        "out = []\n"
        "for z in b.Zones():\n"
        "    if z.GetIsRuleArea():\n"
        "        out.append({'rule_area': True})\n"
        "        continue\n"
        "    out.append({'rule_area': False, 'net': z.GetNetname(),\n"
        "                'layer': b.GetLayerName(z.GetLayer()),\n"
        "                'filled': bool(z.IsFilled()),\n"
        "                'area_mm2': z.GetFilledArea() / 1e12})\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", verify_src, str(board)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _lib.fail(f"zone verification subprocess failed: {proc.stderr.strip()[-300:]}")
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    pour_zones = [z for z in info if not z["rule_area"]]
    unfilled = [z for z in pour_zones if not z["filled"]]
    for z in pour_zones:
        print(
            f"  {(z['net'] or '<no net>'):<8} {z['layer']:<8} "
            f"filled={z['filled']} area={z['area_mm2']:.1f} mm2",
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
        rule_areas=len(info) - len(pour_zones),
        filled=filled,
        filled_area_mm2=round(sum(z["area_mm2"] for z in pour_zones), 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
