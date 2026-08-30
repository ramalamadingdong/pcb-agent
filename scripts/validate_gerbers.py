#!/usr/bin/env python3
"""
validate_gerbers.py - check a KiCad fab package against what you actually meant.

The gerbers are what the factory builds. Nothing else is. This script reads the
exported package (not the KiCad files) and fails loudly on the things that no
other check in the chain catches.

Zero dependencies. Python 3.11+ (needs tomllib).

    python3 validate_gerbers.py --init          # write a starter board.toml
    python3 validate_gerbers.py ./fab/          # check a fab package
    python3 validate_gerbers.py ./fab/ -c board.toml

Exit code is non-zero if any check FAILs, so you can wire it straight into a
build or a pre-order gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    status: str
    check: str
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.results.append(Result(status, check, detail))

    def ok(self, check: str, detail: str = "") -> None:
        self.add(PASS, check, detail)

    def bad(self, check: str, detail: str = "") -> None:
        self.add(FAIL, check, detail)

    def skip(self, check: str, detail: str = "") -> None:
        self.add(SKIP, check, detail)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == FAIL)

    def render(self, color: bool = True) -> str:
        def paint(s: str, c: str) -> str:
            if not color:
                return s
            codes = {"green": "32", "red": "31", "yellow": "33", "dim": "2"}
            return f"\033[{codes[c]}m{s}\033[0m"

        width = max((len(r.check) for r in self.results), default=10)
        lines = []
        for r in self.results:
            tag = {
                PASS: paint("PASS", "green"),
                FAIL: paint("FAIL", "red"),
                SKIP: paint("SKIP", "yellow"),
            }[r.status]
            line = f"{tag}  {r.check.ljust(width)}"
            if r.detail:
                line += "  " + paint(r.detail, "dim")
            lines.append(line)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# gerber parsing
# --------------------------------------------------------------------------


@dataclass
class Aperture:
    code: int
    shape: str
    params: list[float] = field(default_factory=list)
    function: str | None = None

    @property
    def width(self) -> float | None:
        """Effective stroke width when this aperture is used for a draw."""
        if self.shape in ("C", "O") and self.params:
            return self.params[0]
        if self.shape == "R" and len(self.params) >= 2:
            return min(self.params[0], self.params[1])
        return None


@dataclass
class Draw:
    """A stroked segment (D01) — i.e. a trace."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float | None
    net: str | None
    in_region: bool

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass
class Flash:
    """A pad or via placement (D03)."""

    x: float
    y: float
    aperture: Aperture
    net: str | None


@dataclass
class GerberLayer:
    name: str
    path: Path
    draws: list[Draw] = field(default_factory=list)
    flashes: list[Flash] = field(default_factory=list)
    region_points: list[tuple[float, float]] = field(default_factory=list)

    def bbox(self) -> tuple[float, float, float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for d in self.draws:
            xs += [d.x1, d.x2]
            ys += [d.y1, d.y2]
        for f in self.flashes:
            xs.append(f.x)
            ys.append(f.y)
        for x, y in self.region_points:
            xs.append(x)
            ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def all_points(self) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for d in self.draws:
            pts += [(d.x1, d.y1), (d.x2, d.y2)]
        for f in self.flashes:
            pts.append((f.x, f.y))
        pts += self.region_points
        return pts


_EXT_CMD = re.compile(r"%(.+?)\*%", re.S)
_AD = re.compile(r"^ADD(\d+)([A-Za-z_$][\w.$]*|[CROP])[,]?(.*)$")
_COORD = re.compile(r"([XYIJ])([+-]?\d+)")


def parse_gerber(path: Path) -> GerberLayer:
    """A pragmatic RS-274X parser: enough to see traces, pads and nets."""
    text = path.read_text(errors="replace")
    layer = GerberLayer(name=path.name, path=path)

    x_int, x_dec = 4, 6
    scale_mm = 1.0  # multiplier applied after decimal placement
    apertures: dict[int, Aperture] = {}
    current_ap: Aperture | None = None
    current_net: str | None = None
    pending_ap_function: str | None = None
    interpolation = "linear"
    in_region = False
    cx = cy = 0.0
    have_pos = False

    # Split into extended commands and word commands while preserving order.
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in _EXT_CMD.finditer(text):
        chunk = text[pos : m.start()]
        for word in chunk.split("*"):
            word = word.strip()
            if word:
                tokens.append(("word", word))
        tokens.append(("ext", m.group(1)))
        pos = m.end()
    for word in text[pos:].split("*"):
        word = word.strip()
        if word:
            tokens.append(("word", word))

    def to_units(raw: str, decimals: int) -> float:
        neg = raw.startswith("-")
        digits = raw.lstrip("+-")
        val = int(digits) if digits else 0
        if neg:
            val = -abs(val)
        return val / (10**decimals) * scale_mm

    for kind, tok in tokens:
        if kind == "ext":
            # extended commands may be stacked: FSLAX46Y46 / MOMM / ADD10C,0.1
            for sub in tok.split("*"):
                sub = sub.strip()
                if not sub:
                    continue
                if sub.startswith("FS"):
                    m = re.search(r"X(\d)(\d)Y(\d)(\d)", sub)
                    if m:
                        x_int, x_dec = int(m.group(1)), int(m.group(2))
                elif sub.startswith("MO"):
                    scale_mm = 25.4 if "IN" in sub else 1.0
                elif sub.startswith("AD"):
                    m = _AD.match(sub)
                    if m:
                        code = int(m.group(1))
                        shape = m.group(2)
                        params = []
                        for p in m.group(3).split("X"):
                            p = p.strip()
                            try:
                                params.append(float(p) * scale_mm)
                            except ValueError:
                                pass
                        apertures[code] = Aperture(
                            code, shape, params, pending_ap_function
                        )
                elif sub.startswith("TA.AperFunction,"):
                    pending_ap_function = sub.split(",", 1)[1]
                elif sub.startswith("TO.N,"):
                    current_net = sub.split(",", 1)[1]
                elif sub.startswith("TD"):
                    target = sub[2:].strip()
                    if not target or target == ".N":
                        current_net = None
                    if not target or target == ".AperFunction":
                        pending_ap_function = None
                elif sub.startswith("TO") or sub.startswith("TF"):
                    pass
            continue

        word = tok
        if word.startswith("G04"):
            continue
        if word.startswith("G36"):
            in_region = True
            continue
        if word.startswith("G37"):
            in_region = False
            continue
        if word.startswith("G01"):
            interpolation = "linear"
            word = word[3:]
        elif word.startswith("G02") or word.startswith("G03"):
            interpolation = "arc"
            word = word[3:]
        elif word.startswith("G75") or word.startswith("G74"):
            word = word[3:]

        dm = re.search(r"D0?([123])$", word)
        ap_m = re.fullmatch(r"D(\d+)", word)
        if ap_m and int(ap_m.group(1)) >= 10:
            current_ap = apertures.get(int(ap_m.group(1)))
            continue

        coords = dict(_COORD.findall(word))
        nx, ny = cx, cy
        if "X" in coords:
            nx = to_units(coords["X"], x_dec)
        if "Y" in coords:
            ny = to_units(coords["Y"], x_dec)

        if not dm:
            if coords:
                cx, cy, have_pos = nx, ny, True
            continue

        op = dm.group(1)
        if op == "1":  # draw
            if in_region:
                layer.region_points.append((nx, ny))
            else:
                width = current_ap.width if current_ap else None
                if have_pos:
                    layer.draws.append(
                        Draw(cx, cy, nx, ny, width, current_net, in_region)
                    )
            cx, cy, have_pos = nx, ny, True
        elif op == "2":  # move
            if in_region:
                layer.region_points.append((nx, ny))
            cx, cy, have_pos = nx, ny, True
        elif op == "3":  # flash
            if current_ap:
                layer.flashes.append(Flash(nx, ny, current_ap, current_net))
            cx, cy, have_pos = nx, ny, True

    _ = interpolation, x_int
    return layer


# --------------------------------------------------------------------------
# excellon drill parsing
# --------------------------------------------------------------------------


@dataclass
class Hole:
    x: float
    y: float
    diameter: float
    plated: bool


def parse_drill(path: Path) -> list[Hole]:
    text = path.read_text(errors="replace")
    holes: list[Hole] = []
    tools: dict[int, float] = {}
    current = 0.0
    metric = "METRIC" in text or "M71" in text
    scale = 1.0 if metric else 25.4
    plated = "TZ" not in text  # refined below by header
    if re.search(r";\s*TYPE=NON_PLATED", text, re.I) or "NPTH" in path.name.upper():
        plated = False
    else:
        plated = True

    decimals = 3 if metric else 4
    fmt = re.search(r"FORMAT=(\d+):(\d+)", text)
    if fmt:
        decimals = int(fmt.group(2))
    has_decimal_point = "." in text.split("%")[-1][:4000]

    in_body = False
    for line in text.splitlines():
        line = line.strip()
        if line in ("%", "M95"):
            in_body = True
            continue
        m = re.match(r"^T(\d+)C([\d.]+)", line)
        if m:
            tools[int(m.group(1))] = float(m.group(2)) * scale
            continue
        m = re.fullmatch(r"T(\d+)", line)
        if m:
            current = tools.get(int(m.group(1)), 0.0)
            continue
        m = re.match(r"^X([+-]?[\d.]+)Y([+-]?[\d.]+)", line)
        if m and in_body:
            def val(raw: str) -> float:
                if "." in raw or has_decimal_point:
                    return float(raw) * scale
                return int(raw) / (10**decimals) * scale

            holes.append(Hole(val(m.group(1)), val(m.group(2)), current, plated))
    return holes


# --------------------------------------------------------------------------
# fab package discovery
# --------------------------------------------------------------------------

LAYER_ALIASES = {
    "f_cu": "F_Cu",
    "b_cu": "B_Cu",
    "f_silkscreen": "F_Silkscreen",
    "b_silkscreen": "B_Silkscreen",
    "f_silks": "F_Silkscreen",
    "b_silks": "B_Silkscreen",
    "edge_cuts": "Edge_Cuts",
}


def canonical_layer(filename: str) -> str | None:
    stem = Path(filename).stem.lower()
    for key, val in LAYER_ALIASES.items():
        if stem.endswith(key):
            return val
    m = re.search(r"(in\d+_cu)$", stem)
    if m:
        return m.group(1).replace("in", "In").replace("_cu", "_Cu")
    return None


@dataclass
class FabPackage:
    root: Path
    copper: dict[str, GerberLayer] = field(default_factory=dict)
    silk: dict[str, GerberLayer] = field(default_factory=dict)
    outline: GerberLayer | None = None
    holes: list[Hole] = field(default_factory=list)
    drill_files: list[Path] = field(default_factory=list)
    job: dict | None = None
    unknown: list[Path] = field(default_factory=list)
    # None = not configured, True = outline matched, False = mismatch.
    # Coordinate-based checks must not report PASS unless this is True.
    frame_verified: bool | None = None


def load_package(root: Path) -> FabPackage:
    pkg = FabPackage(root=root)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for p in files:
        suffix = p.suffix.lower()
        if suffix == ".gbrjob":
            try:
                pkg.job = json.loads(p.read_text(errors="replace"))
            except Exception:
                pass
            continue
        if suffix in (".drl", ".xln", ".txt") and "drl" in p.name.lower() or suffix == ".drl":
            pkg.drill_files.append(p)
            pkg.holes += parse_drill(p)
            continue
        if suffix not in (".gbr", ".gbl", ".gtl", ".gts", ".gto", ".gm1", ".g2", ".g3"):
            continue
        name = canonical_layer(p.name)
        if name is None:
            pkg.unknown.append(p)
            continue
        layer = parse_gerber(p)
        layer.name = name
        if name == "Edge_Cuts":
            pkg.outline = layer
        elif name.endswith("_Cu"):
            pkg.copper[name] = layer
        elif "Silk" in name:
            pkg.silk[name] = layer
    return pkg


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------


def in_rect(x: float, y: float, r: dict) -> bool:
    x1, x2 = sorted((float(r["x1"]), float(r["x2"])))
    y1, y2 = sorted((float(r["y1"]), float(r["y2"])))
    return x1 <= x <= x2 and y1 <= y <= y2


def seg_hits_rect(d: Draw, r: dict) -> bool:
    if in_rect(d.x1, d.y1, r) or in_rect(d.x2, d.y2, r):
        return True
    # cheap: sample along the segment
    steps = max(2, int(d.length / 0.2))
    for i in range(steps + 1):
        t = i / steps
        if in_rect(d.x1 + (d.x2 - d.x1) * t, d.y1 + (d.y2 - d.y1) * t, r):
            return True
    return False


def net_matches(net: str | None, patterns: list[str]) -> bool:
    if not net:
        return False
    for pat in patterns:
        regex = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        if re.match(regex, net, re.I):
            return True
    return False


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def check_package_sane(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    if not pkg.copper:
        rep.bad("package/copper", f"no copper gerbers found in {pkg.root}")
        return
    rep.ok("package/copper", f"{len(pkg.copper)} layers: {', '.join(sorted(pkg.copper))}")
    if pkg.outline is None:
        rep.bad("package/outline", "no Edge_Cuts gerber — fab has no board shape")
    else:
        bb = pkg.outline.bbox()
        if bb:
            rep.ok("package/outline", f"{bb[2]-bb[0]:.1f} x {bb[3]-bb[1]:.1f} mm")
        else:
            rep.bad("package/outline", "Edge_Cuts is empty")
    if not pkg.drill_files:
        rep.bad("package/drill", "no drill file in the package")
    else:
        rep.ok("package/drill", f"{len(pkg.holes)} holes in {len(pkg.drill_files)} file(s)")
    if pkg.unknown:
        rep.skip(
            "package/unrecognised",
            f"{len(pkg.unknown)} file(s) not matched to a layer: "
            + ", ".join(p.name for p in pkg.unknown[:4]),
        )


def check_frame(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    """Confirm the gerbers sit in the coordinate frame the config assumes.

    Every rectangle in board.toml — keepouts, thermal via zones — is written in
    absolute board coordinates. If the export lands the board somewhere else,
    those rectangles point at empty space, find no copper, and report PASS. A
    keepout that passes because the checker looked in the wrong place is worse
    than no keepout check at all, so nothing coordinate-based is allowed to
    pass until the frame is confirmed.
    """
    frame = cfg.get("frame", {})
    expected = frame.get("outline_bbox")
    if not expected:
        pkg.frame_verified = None
        rep.skip(
            "frame/origin",
            "no [frame] outline_bbox — coordinate checks can't be trusted",
        )
        return
    if pkg.outline is None:
        pkg.frame_verified = False
        rep.bad("frame/origin", "no Edge_Cuts to check the frame against")
        return
    bb = pkg.outline.bbox()
    if bb is None:
        pkg.frame_verified = False
        rep.bad("frame/origin", "Edge_Cuts is empty")
        return
    tol = float(frame.get("tolerance_mm", 0.5))
    exp = [float(v) for v in expected]
    delta = [abs(a - b) for a, b in zip(bb, exp)]
    if max(delta) <= tol:
        pkg.frame_verified = True
        rep.ok("frame/origin", f"outline matches within {max(delta):.3f}mm")
    else:
        pkg.frame_verified = False
        rep.bad(
            "frame/origin",
            f"outline is ({bb[0]:.2f},{bb[1]:.2f})-({bb[2]:.2f},{bb[3]:.2f}), "
            f"config assumes ({exp[0]:.2f},{exp[1]:.2f})-({exp[2]:.2f},{exp[3]:.2f}). "
            "Every keepout and via-zone rectangle is pointing at the wrong place.",
        )


def _frame_note(pkg: FabPackage) -> str | None:
    if pkg.frame_verified is True:
        return None
    if pkg.frame_verified is False:
        return "frame mismatch — this region is not where the config thinks"
    return "frame unverified — set [frame] outline_bbox to trust this"


def check_planes_clean(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    planes = cfg.get("layers", {}).get("planes", [])
    if not planes:
        rep.skip("planes/no-signal", "no plane layers declared in config")
        return
    for pname in planes:
        layer = pkg.copper.get(pname)
        if layer is None:
            rep.bad("planes/no-signal", f"{pname} declared a plane but not in the package")
            continue
        offenders: dict[str, int] = {}
        for d in layer.draws:
            if d.length <= 0:
                continue
            key = d.net or "(no net)"
            offenders[key] = offenders.get(key, 0) + 1
        if offenders:
            total = sum(offenders.values())
            top = ", ".join(sorted(offenders)[:6])
            rep.bad(
                f"planes/{pname}",
                f"{total} track segments across {len(offenders)} nets on a plane layer: {top}",
            )
        else:
            rep.ok(f"planes/{pname}", "no signal draws")


def check_power_widths(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    pw = cfg.get("nets", {}).get("power", {})
    patterns = pw.get("patterns", [])
    min_w = pw.get("min_width_mm")
    if not patterns or not min_w:
        rep.skip("power/width", "no power net rule in config")
        return
    # Nets fed by an inner plane have legitimately thin SURFACE copper — a
    # pad escape into a via is a few mm at pad width. plane_fed exempts
    # those. The discriminator is the CONNECTED CHAIN of thin copper, not
    # segment count: escapes are short isolated stubs, while a routed run —
    # even one drawn as many short segments — links into one long chain.
    # Chains up to stub_max_mm are exempt; longer chains are routed runs
    # and still held to min_width_mm.
    plane_fed = pw.get("plane_fed", [])
    stub_max = float(pw.get("stub_max_mm", 3.0))
    seen: set[str] = set()
    thin: dict[str, float] = {}
    fed_thin: dict[str, list[Draw]] = {}
    for lname, layer in pkg.copper.items():
        for d in layer.draws:
            if not net_matches(d.net, patterns) or d.width is None:
                continue
            assert d.net
            seen.add(d.net)
            if d.width < min_w - 1e-6:
                key = f"{d.net}@{lname}"
                if net_matches(d.net, plane_fed):
                    fed_thin.setdefault(key, []).append(d)
                else:
                    thin[key] = min(thin.get(key, 9e9), d.width)

    stub_note: dict[str, tuple[int, float]] = {}
    for key, draws in fed_thin.items():
        # union-find over shared endpoints (snapped to a 0.05 mm grid)
        parent = list(range(len(draws)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        node: dict[tuple[int, int], int] = {}
        for i, d in enumerate(draws):
            for px, py in ((d.x1, d.y1), (d.x2, d.y2)):
                k = (round(px / 0.05), round(py / 0.05))
                if k in node:
                    a, b = find(node[k]), find(i)
                    parent[a] = b
                else:
                    node[k] = i
        chains: dict[int, float] = {}
        for i, d in enumerate(draws):
            r = find(i)
            chains[r] = chains.get(r, 0.0) + d.length
        worst = max(chains.values())
        if worst > stub_max:
            thin[key] = min(d.width for d in draws)
            thin[key + f" (chain {worst:.1f}mm)"] = thin.pop(key)
        else:
            stub_note[key] = (len(chains), sum(chains.values()))

    if not seen:
        rep.bad(
            "power/width",
            f"no traces matched {patterns} — check your netclass patterns match real net names",
        )
        return
    if thin:
        detail = ", ".join(f"{k} {v:.3f}mm" for k, v in sorted(thin.items())[:6])
        rep.bad("power/width", f"below {min_w}mm: {detail}")
    else:
        note = ""
        if stub_note:
            n_chains = sum(c for c, _ in stub_note.values())
            total = sum(t for _, t in stub_note.values())
            note = (f"; {n_chains} plane-fed escape stub(s) exempted "
                    f"({total:.1f}mm, all chains <= {stub_max}mm)")
        rep.ok("power/width", f"{len(seen)} power nets all >= {min_w}mm{note}")


def check_silk_inside(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    if pkg.outline is None or not pkg.silk:
        rep.skip("silk/inside-outline", "need Edge_Cuts and a silkscreen layer")
        return
    bb = pkg.outline.bbox()
    if bb is None:
        rep.skip("silk/inside-outline", "empty outline")
        return
    margin = float(cfg.get("silkscreen", {}).get("margin_mm", 0.0))
    x1, y1, x2, y2 = bb[0] + margin, bb[1] + margin, bb[2] - margin, bb[3] - margin
    for lname, layer in pkg.silk.items():
        outside = [
            (px, py)
            for px, py in layer.all_points()
            if not (x1 <= px <= x2 and y1 <= py <= y2)
        ]
        if outside:
            sample = ", ".join(f"({px:.2f},{py:.2f})" for px, py in outside[:3])
            rep.bad(
                f"silk/{lname}",
                f"{len(outside)} points outside the board outline — fab will clip: {sample}",
            )
        else:
            rep.ok(f"silk/{lname}", "all inside outline")


def check_mounting_holes(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    mh = cfg.get("mounting_holes", {})
    want = mh.get("count")
    dia = mh.get("diameter_mm")
    if not want or not dia:
        rep.skip("drill/mounting-holes", "no mounting hole rule in config")
        return
    tol = float(mh.get("tolerance_mm", 0.05))
    found = [h for h in pkg.holes if abs(h.diameter - float(dia)) <= tol]
    if len(found) == int(want):
        rep.ok("drill/mounting-holes", f"{len(found)} x {dia}mm")
    else:
        sizes = sorted({round(h.diameter, 3) for h in pkg.holes})
        rep.bad(
            "drill/mounting-holes",
            f"expected {want} at {dia}mm, found {len(found)}. "
            f"Drill sizes present: {sizes[:10]}",
        )


def check_fiducials(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    fd = cfg.get("fiducials", {})
    want = fd.get("count")
    if not want:
        rep.skip("fiducials", "no fiducial rule in config")
        return
    pts: list[tuple[float, float]] = []
    for layer in pkg.copper.values():
        for f in layer.flashes:
            if f.aperture.function and "fiducial" in f.aperture.function.lower():
                pts.append((f.x, f.y))
    if not pts:
        rep.bad(
            "fiducials/count",
            "none found. KiCad tags these via X2 AperFunction — "
            "confirm you exported with X2 attributes on",
        )
        return
    if len(pts) != int(want):
        rep.bad("fiducials/count", f"expected {want}, found {len(pts)}")
    else:
        rep.ok("fiducials/count", f"{len(pts)} found")

    if len(pts) >= 3:
        worst = 0.0
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                for k in range(j + 1, len(pts)):
                    a, b, c = pts[i], pts[j], pts[k]
                    area2 = abs(
                        (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
                    )
                    base = math.hypot(b[0] - a[0], b[1] - a[1])
                    dev = area2 / base if base else 0.0
                    worst = max(worst, dev)
        min_dev = float(fd.get("min_collinear_deviation_mm", 1.0))
        if worst < min_dev:
            rep.bad(
                "fiducials/collinear",
                f"most off-axis triple deviates only {worst:.2f}mm "
                f"(need {min_dev}mm) — assembly can't resolve rotation",
            )
        else:
            rep.ok("fiducials/collinear", f"{worst:.2f}mm off-axis")

    for ko in cfg.get("keepouts", []):
        inside = [p for p in pts if in_rect(p[0], p[1], ko)]
        if inside:
            rep.bad(
                "fiducials/keepout",
                f"{len(inside)} fiducial(s) inside keepout '{ko.get('name','?')}' "
                "(keepout rules ban pours/tracks/vias, not pads)",
            )


def check_keepouts(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    keepouts = cfg.get("keepouts", [])
    if not keepouts:
        rep.skip("keepouts", "none declared in config")
        return
    for ko in keepouts:
        name = ko.get("name", "keepout")
        layers = ko.get("layers") or list(pkg.copper)
        hits: dict[str, int] = {}
        for lname in layers:
            layer = pkg.copper.get(lname)
            if layer is None:
                continue
            n = sum(1 for d in layer.draws if seg_hits_rect(d, ko))
            n += sum(1 for f in layer.flashes if in_rect(f.x, f.y, ko))
            n += sum(1 for x, y in layer.region_points if in_rect(x, y, ko))
            if n:
                hits[lname] = n
        if hits:
            detail = ", ".join(f"{k}:{v}" for k, v in hits.items())
            rep.bad(f"keepouts/{name}", f"copper found on {detail}")
            continue
        note = _frame_note(pkg)
        if note:
            rep.skip(f"keepouts/{name}", f"no copper found, but {note}")
        else:
            rep.ok(f"keepouts/{name}", f"clear on {len(layers)} layer(s)")


def check_thermal_vias(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    zones = cfg.get("thermal_vias", [])
    if not zones:
        rep.skip("thermal-vias", "none declared in config")
        return
    for z in zones:
        name = z.get("name", "zone")
        want = int(z.get("min_count", 1))
        found = sum(1 for h in pkg.holes if h.plated and in_rect(h.x, h.y, z))
        if found < want:
            rep.bad(f"thermal-vias/{name}", f"only {found}, need {want}")
            continue
        note = _frame_note(pkg)
        if note:
            rep.skip(f"thermal-vias/{name}", f"{found} found, but {note}")
        else:
            rep.ok(f"thermal-vias/{name}", f"{found} (need {want})")


def check_rf_budget(pkg: FabPackage, cfg: dict, rep: Report) -> None:
    budgets = cfg.get("rf_budget", [])
    if not budgets:
        rep.skip("rf/length", "no RF budget in config")
        return
    for b in budgets:
        net = b["net"]
        limit = float(b["max_length_mm"])
        total = 0.0
        for layer in pkg.copper.values():
            for d in layer.draws:
                if net_matches(d.net, [net]):
                    total += d.length
        if total == 0:
            rep.bad(f"rf/{net}", "no copper found for this net — check the name")
        elif total > limit:
            rep.bad(f"rf/{net}", f"{total:.1f}mm routed, budget {limit}mm")
        else:
            rep.ok(f"rf/{net}", f"{total:.1f}mm of {limit}mm")


CHECKS = [
    check_package_sane,
    check_frame,
    check_planes_clean,
    check_power_widths,
    check_silk_inside,
    check_mounting_holes,
    check_fiducials,
    check_keepouts,
    check_thermal_vias,
    check_rf_budget,
]


# --------------------------------------------------------------------------
# starter config
# --------------------------------------------------------------------------

STARTER = '''\
# board.toml — what this board is supposed to be.
# Every rule you add here is one you never have to remember again.
# Anything you catch by hand, add as a rule. That is the whole point.

[board]
name = "my-board"

[layers]
# Inner layers you declared solid planes. Any track on these is a FAIL:
# a signal here breaks the reference plane for everything above it.
planes = ["In1_Cu", "In2_Cu"]

[nets.power]
# The generated netclass patterns (VCC*, VDD*, +*V) match none of the usual
# names, so power traces come out at default width and nothing errors.
patterns = ["VBUS", "VSYS", "VBAT", "+3V3", "+5V", "GND", "VDD*", "VCC*"]
min_width_mm = 0.30
# Nets fed by an inner plane have legitimately thin surface stubs (a pad
# escape into a via). Listing them here exempts CONNECTED CHAINS of thin
# copper up to stub_max_mm; longer chains are routed runs — even chopped
# into short segments they stay connected — and still fail min_width_mm.
# plane_fed = ["GND", "+3V3"]
# stub_max_mm = 3.0
# Nets fed by an inner plane have legitimately thin surface stubs (a pad
# escape into a via). List them here to exempt segments up to stub_max_mm;
# longer runs are still held to min_width_mm, and more than
# stub_total_max_mm of exempted copper on one net+layer fails anyway.
# plane_fed = ["GND", "+3V3"]
# stub_max_mm = 2.0
# stub_total_max_mm = 10.0

[frame]
# Guards against the checker inspecting the wrong region. Every rectangle
# below is in absolute board coordinates; if the export lands the board
# somewhere else, they point at empty space and report PASS. Fill in the
# Edge_Cuts bounding box you expect. Without this, keepout and via-zone
# checks report SKIP rather than PASS — a check that looked in the wrong
# place is not a check that passed.
# outline_bbox = [0.0, 0.0, 68.58, 53.34]
# tolerance_mm = 0.5

[silkscreen]
# Fab clips anything past the outline, so it just prints bare.
margin_mm = 0.0

[mounting_holes]
# Not netlist objects — nothing emits them and nothing checks them.
count = 4
diameter_mm = 3.2
tolerance_mm = 0.05

[fiducials]
count = 3
min_collinear_deviation_mm = 1.0

# Keepouts ban pours, tracks and vias — but not pads. Declare them here and
# this checks all layers, pads included.
# [[keepouts]]
# name = "antenna"
# x1 = 100.0
# y1 = 80.0
# x2 = 112.0
# y2 = 88.0
# layers = ["F_Cu", "In1_Cu", "In2_Cu", "B_Cu"]

# [[thermal_vias]]
# name = "module_exposed_pad"
# x1 = 120.0
# y1 = 90.0
# x2 = 130.0
# y2 = 98.0
# min_count = 9

# [[rf_budget]]
# net = "ANT"
# max_length_mm = 25.0
'''


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="validate_gerbers",
        description="Check a KiCad fab package against what you meant to build.",
    )
    ap.add_argument("package", nargs="?", help="directory containing the exported gerbers")
    ap.add_argument("-c", "--config", default="board.toml", help="rules file")
    ap.add_argument("--init", action="store_true", help="write a starter board.toml and exit")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)

    if args.init:
        dest = Path(args.config)
        if dest.exists():
            print(f"{dest} already exists — not overwriting.", file=sys.stderr)
            return 1
        dest.write_text(STARTER)
        print(f"Wrote {dest}. Edit it, then run:\n  python3 {sys.argv[0]} ./fab/")
        return 0

    if not args.package:
        ap.print_help()
        return 2

    root = Path(args.package)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    cfg: dict = {}
    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = tomllib.loads(cfg_path.read_text())
    else:
        print(
            f"No {cfg_path} found — running structural checks only.\n"
            f"Run with --init to create one.\n",
            file=sys.stderr,
        )

    pkg = load_package(root)
    rep = Report()
    for check in CHECKS:
        try:
            check(pkg, cfg, rep)
        except Exception as exc:  # a broken check must not hide the others
            rep.bad(check.__name__, f"checker crashed: {exc!r}")

    color = not args.no_color and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    name = cfg.get("board", {}).get("name", root.name)
    print(f"\n{name} — {root}\n")
    print(rep.render(color=color))
    passed = sum(1 for r in rep.results if r.status == PASS)
    skipped = sum(1 for r in rep.results if r.status == SKIP)
    parts = [f"{passed} passed"]
    if skipped:
        parts.append(f"{skipped} not checked")
    parts.append(f"{rep.failed} failed")
    print("\n" + ", ".join(parts) + "\n")
    if skipped and not rep.failed:
        print("Some checks did not run. A rule you never configured is not a\n"
              "rule that passed — see the SKIP lines above.\n")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
