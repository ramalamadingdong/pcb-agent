#!/usr/bin/env python3
"""
Dogbone fanout for the fine-pitch parts, so the autorouter has somewhere to start.

The autorouter cannot route a board like this because it cannot get OUT of the
fine-pitch packages:

    an LGA-14     0.35 mm pads on 0.5 mm pitch -> 0.15 mm between pads.
                  Threading a trace costs clearance+width+clearance, i.e.
                  0.30 mm even at 0.1/0.1 rules, so NO cheap fab tier lets a
                  trace pass between two of its pads.  It is fanout-only.
    a 1.27 mm-pitch module   0.9 mm pads -> 0.37 mm between pads.
                  0.15/0.15 needs 0.45 mm, so the same problem, milder.

The router's own escape stage wants to solve this with via-in-pad, which the
cheap fab tiers forbid, so it rolls back ("Relief rescue: ... has NO relief
path").

This pass does what a human does instead: run a short stub straight out of each
pad, perpendicular to the package edge, and drop a via just outside the pad ring
-- a dogbone.  Vias alternate between two rings so the via pitch is twice the
pad pitch and they clear each other.  After this the router only has to solve
via-to-via in open copper, which is a completely different problem.

Run after floorplan/placement and after the zone pours.  The pass is
idempotent: it strips all existing segments/vias (keeping zones) before it
places anything, so it can be re-run without stacking duplicate copper.

--------------------------------------------------------------------------
CLEARANCE MODEL -- read before changing any geometry constant
--------------------------------------------------------------------------
The first version of this script budgeted only two spacings: pad-edge to its
OWN via, and via to via.  That is not sufficient, and it shipped a board with
10 DRC errors that all looked like unrelated footprint problems:

  * A ring-B stub runs radially outward PAST its neighbour's ring-A via, one
    pad pitch (0.5 mm) to the side.  The real spacing is
        pitch - track_width/2 - via_size/2
    which for the LGA at via 0.6 / track 0.20 is exactly 0.100 mm against a
    0.15 mm rule -- the six identical "clearance 0.1500; actual 0.1000"
    errors.  Shrinking that part's vias to 0.45 mm makes it 0.175 mm.
  * A via was dropped on top of a NEIGHBOURING FOOTPRINT's pad (a module's
    strapping-pin escape landed on a nearby resistor's pad 2), because
    nothing checked foreign copper at all -- one short, one clearance error,
    one hole_clearance, one mask bridge.

So every candidate via AND its stub is now checked against all previously
placed vias, all previously placed stubs, and every pad on the board that is
not on the same net.  A pad that cannot clear at its natural ring steps
outward by one via pitch and retries before being given up on.

Keep clearance_mm at or above the largest netclass clearance the escape can
touch: if the project sets Default/Power/Ground/Clock to 0.15 mm, and DRC
enforces the LARGER of the two nets' clearances, then a 0.1 mm Control-class
net escaping past a Ground via is still judged at 0.15 mm.

board.toml
----------
::

    [fanout]
    clearance_mm  = 0.15    # copper-to-copper design target (see above)
    max_ring_steps = 3      # via-pitch steps outward before giving up
    eps_mm        = 0.001

    # One entry per fine-pitch part. This is what the source pipeline keyed
    # on: an explicit, reviewed recipe per package.
    [[fanout.target]]
    ref             = "U2"
    track_width_mm  = 0.20
    via_size_mm     = 0.45
    via_drill_mm    = 0.20
    two_rings       = true
    skip_pads       = ["29"]        # handled elsewhere, e.g. a via field

    # Exposed-pad / thermal via fields: one via dropped concentric with the
    # named pad, no stub.
    [[fanout.thermal_vias]]
    ref          = "U1"
    pad          = "29"
    net          = "GND"
    via_size_mm  = 0.6
    via_drill_mm = 0.3

    # Optional auto-discovery, off unless auto_pitch_below_mm is present. Any
    # footprint with at least auto_min_pads pads whose closest pad-centre
    # spacing is below auto_pitch_below_mm gets a target derived from the
    # clearance model above rather than hand-written. The pad-count floor is
    # load-bearing: an 0402 has ~0.5 mm between its two pad centres and would
    # otherwise be "discovered" as a fine-pitch package and fanned out.
    [fanout]
    auto_pitch_below_mm    = 0.8
    auto_min_pads          = 8
    default_track_width_mm = 0.20
    default_via_size_mm    = 0.60
    default_via_drill_mm   = 0.30
    min_via_size_mm        = 0.45
    min_via_drill_mm       = 0.20
    min_annular_ring_mm    = 0.10

Usage:
    python3 fanout.py --board board.kicad_pcb --config board.toml
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from kicad_tools.pcb.editor import PCBEditor
from kicad_tools.schema.pcb import PCB

import _lib

PASS = "fanout"

# Geometry is derived per pad rather than hard-coded, so a footprint change can
# not silently invalidate a magic constant.  These are defaults; board.toml
# overrides them.  Confirm they sit above your fab's floor (a typical cheap
# tier encodes 0.1016 mm clearance, 0.2 mm hole, 0.45 mm via).
CLEARANCE_DEFAULT = 0.15   # mm, copper-to-copper design target

# How many via-pitch steps outward a blocked pad may try before we give up.
MAX_RING_STEPS_DEFAULT = 3

# Floating-point slack, in mm.  DRC compares at file precision (1 nm); we keep
# a micron of margin so a value that computes to exactly CLEARANCE does not
# land on the wrong side of the comparison after rounding to 4 decimals.
EPS_DEFAULT = 0.001

COPPER_LAYERS = ("F.Cu", "B.Cu", "*.Cu")


@dataclass(frozen=True)
class Target:
    """Fanout recipe for one fine-pitch package.

    via_size is the lever that makes a 0.5 mm-pitch part legal: the binding
    constraint is a ring-B stub passing a ring-A via at one pad pitch, i.e.
    ``pitch - track_width/2 - via_size/2 >= CLEARANCE``.
    """

    track_width: float
    two_rings: bool
    via_size: float
    via_drill: float
    # Pads handled elsewhere (an exposed pad is a via field, not a dogbone).
    skip_pads: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class ThermalVias:
    """A via field: one via per land of a named (usually split) pad."""

    ref: str
    pad: str
    net: str | None
    via_size: float
    via_drill: float


# Nets that must not be fanned out: single-pad no-connects carry an
# auto-generated "unconnected-(...)" name and have nowhere to go.
def is_routable(net_name: str) -> bool:
    return bool(net_name) and not net_name.startswith("unconnected-")


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a footprint-local offset into board orientation (KiCad, CW +Y down)."""
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c + y * s, -x * s + y * c)


# --- geometry primitives ---------------------------------------------------
# Everything below is exact for axis-aligned rectangles.  Pads are reduced to
# the axis-aligned bounding box of their rotated corners, which is exact for
# 0/90/180/270 rotations and conservative otherwise.


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def dist_point_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = _clamp(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_point_rect(px: float, py: float, cx: float, cy: float, hw: float, hh: float) -> float:
    dx = max(abs(px - cx) - hw, 0.0)
    dy = max(abs(py - cy) - hh, 0.0)
    return math.hypot(dx, dy)


def _ccw(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _seg_seg_hit(a0, a1, b0, b1) -> bool:
    d1 = _ccw(*a0, *a1, *b0)
    d2 = _ccw(*a0, *a1, *b1)
    d3 = _ccw(*b0, *b1, *a0)
    d4 = _ccw(*b0, *b1, *a1)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def dist_seg_seg(a0, a1, b0, b1) -> float:
    if _seg_seg_hit(a0, a1, b0, b1):
        return 0.0
    return min(
        dist_point_seg(*a0, *b0, *b1),
        dist_point_seg(*a1, *b0, *b1),
        dist_point_seg(*b0, *a0, *a1),
        dist_point_seg(*b1, *a0, *a1),
    )


def dist_seg_rect(a0, a1, cx: float, cy: float, hw: float, hh: float) -> float:
    """Exact minimum distance between a segment and an axis-aligned rectangle.

    For two convex shapes the minimum is attained at a vertex of one measured
    against the other, so checking both endpoints against the rect and all four
    corners against the segment is sufficient once overlap is ruled out.
    """
    if dist_point_rect(*a0, cx, cy, hw, hh) == 0.0 or dist_point_rect(*a1, cx, cy, hw, hh) == 0.0:
        return 0.0
    corners = [
        (cx - hw, cy - hh),
        (cx + hw, cy - hh),
        (cx + hw, cy + hh),
        (cx - hw, cy + hh),
    ]
    for i in range(4):
        if _seg_seg_hit(a0, a1, corners[i], corners[(i + 1) % 4]):
            return 0.0
    d = min(
        dist_point_rect(*a0, cx, cy, hw, hh),
        dist_point_rect(*a1, cx, cy, hw, hh),
    )
    for c in corners:
        d = min(d, dist_point_seg(*c, *a0, *a1))
    return d


# --- obstacle model --------------------------------------------------------


@dataclass(frozen=True)
class PadObstacle:
    net: str
    cx: float
    cy: float
    hw: float
    hh: float
    ref: str
    number: str


@dataclass(frozen=True)
class ViaObstacle:
    net: str
    x: float
    y: float
    r: float


@dataclass(frozen=True)
class TrackObstacle:
    net: str
    a: tuple
    b: tuple
    half_w: float
    layer: str


def collect_pads(pcb: PCB, ox: float, oy: float) -> list[PadObstacle]:
    """Every copper pad on the board, in board coordinates."""
    out: list[PadObstacle] = []
    for fp in pcb.footprints:
        fx, fy = fp.position[0] + ox, fp.position[1] + oy
        for pad in fp.pads:
            if not any(layer in COPPER_LAYERS for layer in pad.layers):
                continue
            ang = fp.rotation + (pad.rotation or 0.0)
            sw, sh = pad.size
            corners = [
                rotate(sx * sw / 2.0, sy * sh / 2.0, ang)
                for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
            ]
            hw = max(abs(x) for x, _ in corners)
            hh = max(abs(y) for _, y in corners)
            px, py = rotate(pad.position[0], pad.position[1], fp.rotation)
            out.append(
                PadObstacle(
                    net=pad.net_name or "",
                    cx=fx + px,
                    cy=fy + py,
                    hw=hw,
                    hh=hh,
                    ref=fp.reference,
                    number=pad.number,
                )
            )
    return out


class Obstacles:
    """Clearance oracle for candidate fanout copper.

    Same-net copper is ignored -- touching your own net is not a violation.
    """

    def __init__(self, pads: list[PadObstacle], clearance: float, eps: float) -> None:
        self.pads = pads
        self.clearance = clearance
        self.eps = eps
        self.vias: list[ViaObstacle] = []
        self.tracks: list[TrackObstacle] = []

    def blocker(
        self,
        net: str,
        via_xy: tuple,
        via_r: float,
        stub: tuple | None,
        half_w: float,
        layer: str = "F.Cu",
    ):
        """Return a short reason string for the first violation found, else None.

        FOUR spacings, not two.  The via against foreign pads, the STUB
        against foreign pads, the via against foreign vias, and -- the one
        the first version missed -- the STUB running radially outward past
        the NEXT pad's via.  DRC enforces the larger of the two nets'
        clearances, so a fine-pitch escape passing ground copper is held to
        the ground net's rule, not its own.

        Vias are through-hole, so every via test is layer-agnostic; only the
        stub-against-stub test is filtered to one copper layer.
        """
        need = self.clearance + self.eps

        for p in self.pads:
            if p.net == net:
                continue
            if dist_point_rect(*via_xy, p.cx, p.cy, p.hw, p.hh) - via_r < need:
                return f"via vs pad {p.ref}.{p.number} [{p.net or 'nonet'}]"
            if stub is not None and dist_seg_rect(*stub, p.cx, p.cy, p.hw, p.hh) - half_w < need:
                return f"stub vs pad {p.ref}.{p.number} [{p.net or 'nonet'}]"

        for v in self.vias:
            if v.net == net:
                continue
            if math.hypot(via_xy[0] - v.x, via_xy[1] - v.y) - via_r - v.r < need:
                return f"via vs via [{v.net}]"
            if stub is not None and dist_point_seg(v.x, v.y, *stub[0], *stub[1]) - half_w - v.r < need:
                return f"stub vs via [{v.net}]"

        for t in self.tracks:
            if t.net == net:
                continue
            if dist_point_seg(*via_xy, *t.a, *t.b) - via_r - t.half_w < need:
                return f"via vs stub [{t.net}]"
            if (
                stub is not None
                and t.layer == layer
                and dist_seg_seg(*stub, t.a, t.b) - half_w - t.half_w < need
            ):
                return f"stub vs stub [{t.net}]"

        return None

    def commit(
        self,
        net: str,
        via_xy: tuple,
        via_r: float,
        stub: tuple | None,
        half_w: float,
        layer: str = "F.Cu",
    ) -> None:
        self.vias.append(ViaObstacle(net, via_xy[0], via_xy[1], via_r))
        if stub is not None:
            self.tracks.append(TrackObstacle(net, stub[0], stub[1], half_w, layer))


# --- config ----------------------------------------------------------------


def copper_pad_points(fp) -> list[tuple[float, float]]:
    pts = []
    for pad in fp.pads:
        if not any(layer in COPPER_LAYERS for layer in pad.layers):
            continue
        pts.append(rotate(pad.position[0], pad.position[1], fp.rotation))
    return pts


def local_pad_pitch(pts: list[tuple[float, float]]) -> float | None:
    """Closest centre-to-centre spacing between two copper pads, in mm.

    This is the ``pitch`` the clearance model above is written against.
    """
    if len(pts) < 2:
        return None
    best = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if d > 0 and (best is None or d < best):
                best = d
    return best


def derive_target(pitch: float, fcfg: dict, clearance: float) -> tuple[Target | None, str]:
    """Solve the clearance model for a via that fits this pitch.

    Straight out of the model above:  pitch - track/2 - via/2 >= CLEARANCE,
    so  via <= 2*(pitch - CLEARANCE - track/2).  Two rings are needed as soon
    as adjacent vias on ONE ring cannot clear each other, i.e. when
    pitch < via + CLEARANCE.
    """
    track = float(fcfg.get("default_track_width_mm", 0.20))
    via_pref = float(fcfg.get("default_via_size_mm", 0.60))
    via_min = float(fcfg.get("min_via_size_mm", 0.45))
    drill_min = float(fcfg.get("min_via_drill_mm", 0.20))
    annular_min = float(fcfg.get("min_annular_ring_mm", 0.10))

    via_max = 2.0 * (pitch - clearance - track / 2.0)
    # Round to the micron BEFORE flooring to the 10-micron grid. At a 0.5 mm
    # pitch with 0.15 clearance and a 0.2 track, via_max is 0.49999999999999994
    # in binary, floor() makes that 0.49, and a min_via_size_mm of exactly 0.50
    # -- the board's own DRC floor -- refuses the part with "give this part an
    # explicit [[fanout.target]]". Three fine-pitch packages silently lost
    # their fanout to one ulp.
    via = min(via_pref, math.floor(round(via_max, 6) * 100 + 1e-9) / 100.0)
    if via < via_min:
        return None, (
            f"pitch {pitch:.3f} mm allows only a {via_max:.3f} mm via at "
            f"track {track} / clearance {clearance}, below min_via_size_mm "
            f"{via_min} — give this part an explicit [[fanout.target]]"
        )
    drill = min(float(fcfg.get("default_via_drill_mm", 0.30)), via - 2.0 * annular_min)
    drill = math.floor(drill * 100) / 100.0
    if drill < drill_min:
        return None, (
            f"a {via:.2f} mm via leaves only a {drill:.2f} mm drill at "
            f"{annular_min} mm annular ring, below min_via_drill_mm {drill_min}"
        )
    return Target(track, pitch < via + clearance, via, drill), ""


def load_targets(cfg: dict, pcb: PCB, clearance: float) -> tuple[dict[str, Target], list[str]]:
    """Explicit [[fanout.target]] entries first, then optional auto-discovery.

    An explicit entry always wins: it is the reviewed recipe.
    """
    fcfg = cfg.get("fanout", {}) or {}
    targets: dict[str, Target] = {}
    notes: list[str] = []

    for i, t in enumerate(fcfg.get("target", []) or []):
        missing = [
            k
            for k in ("ref", "track_width_mm", "via_size_mm", "via_drill_mm")
            if k not in t
        ]
        if missing:
            _lib.fail(
                f"[[fanout.target]] #{i + 1} is missing " + ", ".join(missing)
            )
        targets[str(t["ref"])] = Target(
            track_width=float(t["track_width_mm"]),
            two_rings=bool(t.get("two_rings", False)),
            via_size=float(t["via_size_mm"]),
            via_drill=float(t["via_drill_mm"]),
            skip_pads=frozenset(str(p) for p in t.get("skip_pads", [])),
        )

    auto_below = fcfg.get("auto_pitch_below_mm")
    if auto_below is not None:
        # An 0402's two pad centres sit ~0.5 mm apart, which is "fine pitch"
        # by spacing and emphatically not a package needing a dogbone escape.
        # The pad-count floor is what keeps auto-discovery off every passive
        # on the board.
        min_pads = int(fcfg.get("auto_min_pads", 8))
        for fp in pcb.footprints:
            ref = fp.reference
            if ref in targets:
                continue
            pts = copper_pad_points(fp)
            if len(pts) < min_pads:
                continue
            pitch = local_pad_pitch(pts)
            if pitch is None or pitch >= float(auto_below):
                continue
            spec, why = derive_target(pitch, fcfg, clearance)
            if spec is None:
                notes.append(f"{ref}: auto-fanout declined ({why})")
                continue
            targets[ref] = spec
            notes.append(
                f"{ref}: auto target from {pitch:.3f} mm pitch — via "
                f"{spec.via_size}/{spec.via_drill}, track {spec.track_width}, "
                f"{'two rings' if spec.two_rings else 'one ring'}"
            )

    return targets, notes


def load_thermal_vias(cfg: dict) -> list[ThermalVias]:
    out = []
    for i, t in enumerate(cfg.get("fanout", {}).get("thermal_vias", []) or []):
        missing = [k for k in ("ref", "pad") if k not in t]
        if missing:
            _lib.fail(
                f"[[fanout.thermal_vias]] #{i + 1} is missing " + ", ".join(missing)
            )
        out.append(
            ThermalVias(
                ref=str(t["ref"]),
                pad=str(t["pad"]),
                net=str(t["net"]) if "net" in t else None,
                via_size=float(t.get("via_size_mm", 0.6)),
                via_drill=float(t.get("via_drill_mm", 0.3)),
            )
        )
    return out


# --- main ------------------------------------------------------------------


def main() -> int:
    args = _lib.pass_parser(PASS).parse_args()
    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no such board")

    cfg = _lib.load_config(args.config)
    fcfg = cfg.get("fanout", {}) or {}
    clearance = float(fcfg.get("clearance_mm", CLEARANCE_DEFAULT))

    # An escape stub on a POWER pad is laid at the power width, not the
    # target's signal width. At 0.20 mm every stub on +1V8, +3V3, +5V_SYS and
    # VBAT_PROT failed validate_gerbers' power/width check straight off the
    # plotted bytes, and a GND chain ran past the plane-fed exemption. The
    # stub leaves the pad along its long axis, away from the neighbours, so
    # the extra width costs nothing in the ring model -- half_w below is the
    # per-pad value, so the blocker sees the true copper.
    power_patterns = list((cfg.get("nets", {}).get("power", {}) or {})
                          .get("patterns") or [])
    power_w = float((cfg.get("route", {}) or {}).get("power_track_width_mm") or 0.0)

    def is_power(net_name: str) -> bool:
        return bool(net_name) and any(fnmatch(net_name, p) for p in power_patterns)
    max_ring_steps = int(fcfg.get("max_ring_steps", MAX_RING_STEPS_DEFAULT))
    eps = float(fcfg.get("eps_mm", EPS_DEFAULT))

    pcb = PCB.load(board)

    # Idempotence: drop every segment and via, keep the pours.  Without this a
    # second run stacks a duplicate fanout on top of the first.
    stripped = pcb.strip_traces(keep_zones=True)
    if stripped["segments"] or stripped["vias"]:
        pcb.save(board)
        print(
            f"  stripped {stripped['segments']} segments, {stripped['vias']} vias",
            file=sys.stderr,
        )
        _lib.assert_net_table(board)
        pcb = PCB.load(board)

    targets, notes = load_targets(cfg, pcb, clearance)
    for n in notes:
        print(f"  {n}", file=sys.stderr)
    thermals = load_thermal_vias(cfg)
    if not targets and not thermals:
        _lib.fail(
            f"{args.config}: no [[fanout.target]] and no [[fanout.thermal_vias]] "
            "entries, and no [fanout] auto_pitch_below_mm — nothing to fan out"
        )

    ed = PCBEditor(str(board))

    ox, oy = 0.0, 0.0
    for fp in pcb.footprints:
        ox, oy = fp._board_origin
        break

    obstacles = Obstacles(collect_pads(pcb, ox, oy), clearance, eps)
    total_vias = total_tracks = skipped = stepped = 0
    per_ref: dict[str, int] = {}
    missing_refs: list[str] = []

    for ref in sorted(targets):
        spec = targets[ref]
        fp = next((f for f in pcb.footprints if f.reference == ref), None)
        if fp is None:
            print(f"  {ref}: not on board, skipped", file=sys.stderr)
            missing_refs.append(ref)
            continue

        fx = fp.position[0] + ox
        fy = fp.position[1] + oy
        via_r = spec.via_size / 2.0
        half_w = spec.track_width / 2.0
        # The escape stub lives on the side the part is mounted on.  (The
        # source pipeline hard-coded F.Cu because that board had no
        # bottom-side parts; a B.Cu part's dogbone on F.Cu is a short.)
        esc_layer = "B.Cu" if str(fp.layer) == "B.Cu" else "F.Cu"

        # Pad ring half-extent, in board orientation.
        pads = []
        for pad in fp.pads:
            px, py = rotate(pad.position[0], pad.position[1], fp.rotation)
            sw, sh = pad.size
            if fp.rotation % 180 == 90:
                sw, sh = sh, sw
            pads.append((pad, px, py, sw, sh))
        if not pads:
            continue
        hx = max(abs(p[1]) for p in pads)
        hy = max(abs(p[2]) for p in pads)

        # Deterministic order so ring assignment is stable across runs.
        pads.sort(key=lambda p: (p[0].number.zfill(4)))

        placed_here = 0
        ring_toggle = 0
        for pad, px, py, sw, sh in pads:
            if not is_routable(pad.net_name) or pad.number in spec.skip_pads:
                continue

            # Which package edge is this pad on?  Compare normalised distance so
            # a tall thin package still classifies its corner pads sensibly.
            on_vertical_edge = (abs(px) / hx if hx else 0) >= (abs(py) / hy if hy else 0)

            # Ring A puts the via edge exactly CLEARANCE beyond this pad's own
            # edge; ring B steps out by one via pitch so alternating pads never
            # crowd each other.
            pad_half = (sw if on_vertical_edge else sh) / 2.0
            base_ring = pad_half + clearance + via_r
            if spec.two_rings and ring_toggle:
                base_ring += spec.via_size + clearance
            ring_toggle ^= 1

            pad_xy = (round(fx + px, 4), round(fy + py, 4))
            step = spec.via_size + clearance
            stub_w = max(spec.track_width, power_w) if is_power(pad.net_name) \
                else spec.track_width
            half_w = stub_w / 2.0

            placed = False
            reason = "no candidate"
            for k in range(max_ring_steps + 1):
                ring = base_ring + k * step
                if on_vertical_edge:
                    vx = px + math.copysign(ring, px if px else 1.0)
                    vy = py
                else:
                    vx = px
                    vy = py + math.copysign(ring, py if py else 1.0)

                via_xy = (round(fx + vx, 4), round(fy + vy, 4))
                stub = (pad_xy, via_xy)

                reason = obstacles.blocker(
                    pad.net_name, via_xy, via_r, stub, half_w, esc_layer
                )
                if reason is not None:
                    continue

                ed.add_via(via_xy, pad.net_name, drill=spec.via_drill, size=spec.via_size)
                ed.add_track(
                    pad.net_name,
                    [pad_xy, via_xy],
                    width=stub_w,
                    layer=esc_layer,
                )
                obstacles.commit(
                    pad.net_name, via_xy, via_r, stub, half_w, esc_layer
                )
                total_vias += 1
                total_tracks += 1
                placed_here += 1
                if k:
                    stepped += 1
                placed = True
                break

            if not placed:
                skipped += 1
                print(
                    f"    {ref}.{pad.number} [{pad.net_name}] skipped: {reason}",
                    file=sys.stderr,
                )

        per_ref[ref] = placed_here
        rings = "two rings" if spec.two_rings else "one ring"
        print(
            f"  {ref}: fanned out ({rings}, via {spec.via_size}/{spec.via_drill} mm)",
            file=sys.stderr,
        )

    # --- exposed-pad / thermal via fields ---------------------------------
    # A module's exposed pad is often several separate lands in the vendor
    # footprint, and the reference layout puts a via in each.  Same clearance
    # oracle, no stub.
    for tv in thermals:
        fp = next((f for f in pcb.footprints if f.reference == tv.ref), None)
        if fp is None:
            print(f"  {tv.ref}: not on board, thermal vias skipped", file=sys.stderr)
            missing_refs.append(tv.ref)
            continue
        via_r = tv.via_size / 2.0
        for pad in fp.pads:
            if pad.number != tv.pad:
                continue
            if tv.net is not None and pad.net_name != tv.net:
                continue
            net = pad.net_name or tv.net or ""
            if not is_routable(net):
                continue
            px, py = rotate(pad.position[0], pad.position[1], fp.rotation)
            via_xy = (
                round(fp.position[0] + ox + px, 4),
                round(fp.position[1] + oy + py, 4),
            )
            reason = obstacles.blocker(net, via_xy, via_r, None, 0.0)
            if reason is not None:
                skipped += 1
                print(
                    f"    {tv.ref}.{tv.pad} thermal via skipped: {reason}",
                    file=sys.stderr,
                )
                continue
            ed.add_via(via_xy, net, drill=tv.via_drill, size=tv.via_size)
            obstacles.commit(net, via_xy, via_r, None, 0.0)
            total_vias += 1
            per_ref[tv.ref] = per_ref.get(tv.ref, 0) + 1

    ed.save()
    _lib.assert_net_table(board)

    print(
        f"Fanout: {total_vias} vias, {total_tracks} escape stubs, "
        f"{stepped} stepped outward, {skipped} skipped (no clear ring)",
        file=sys.stderr,
    )

    _lib.emit(
        PASS,
        board=str(board),
        clearance_mm=clearance,
        stripped_segments=stripped["segments"],
        stripped_vias=stripped["vias"],
        vias=total_vias,
        stubs=total_tracks,
        stepped_outward=stepped,
        skipped=skipped,
        per_ref=per_ref,
        refs_not_on_board=missing_refs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
