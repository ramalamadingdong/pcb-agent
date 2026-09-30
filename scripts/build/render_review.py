#!/usr/bin/env python3
"""Draw the board for REVIEW: one picture per copper layer, with the ratsnest.

Level 3 of the checking (the agent looking at the board) had no picture to
look at. kicad-cli can plot layers, but its plots carry no ratsnest and no
labels, and the question a reviewer (or /improve-placement) asks is exactly
"where is it stuck, and between which parts?". This draws:

  * the outline, zone fills (faint), tracks, vias and pads, per copper layer,
    plus an "all" panel with every layer stacked;
  * the refdes of every footprint;
  * the ratsnest: every net still in pieces, as RED lines joining its islands
    (a minimum spanning tree over the islands, closest pads first), labelled
    with the net name;
  * with --route-report: the nets the ROUTER left unrouted but the completion
    passes finished, their copper drawn ORANGE. That copper came from
    finish_routes, not Freerouting, and deserves the closer look;
  * segments not at a multiple of 45 deg, in MAGENTA.

It writes PNGs when Pillow is importable (KiCad's bundled Python ships it)
and SVGs otherwise. It reads the board and writes nothing to it.

The approach (a dark KiCad-like palette, per-layer panels, an MST ratsnest)
is adapted from the board renderer in paul356/KiCad-AI-Assistant
(`kcaa/tools/render_board_tools.py`, MIT, (c) 2025 Lama Al Rajih). No code is
copied: geometry here comes from pcbnew itself (pad polygons through
TransformShapeToPolygon), so pad rotation is whatever the board says, which
is the point of looking.

Usage::

    render_review.py --board b.kicad_pcb [--route-report build/route.json]
                     [--out review/] [--scale 20]

--netlist and --config are accepted for contract uniformity and unused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

PASS = "render_review"

BG = (17, 17, 20, 255)
OUTLINE = (230, 230, 120, 255)
LAYER_RGB = {
    "F.Cu": (200, 52, 52), "B.Cu": (60, 90, 200),
    "In1.Cu": (60, 160, 90), "In2.Cu": (200, 150, 40),
}
FALLBACK_RGB = (160, 160, 160)
VIA = (200, 200, 200, 255)
HOLE = (17, 17, 20, 255)
TEXT = (235, 235, 235, 255)
RAT = (255, 40, 40, 255)
FINISHED = (255, 150, 0, 255)
OFF_ANGLE = (255, 0, 255, 255)


class Canvas:
    """Primitives in board mm; rendered to PNG (Pillow) or SVG."""

    def __init__(self, box: tuple[float, float, float, float], scale: float) -> None:
        self.x0, self.y0 = box[0] - 2, box[1] - 6       # room for the title
        self.w, self.h = box[2] - box[0] + 4, box[3] - box[1] + 8
        self.scale = scale
        self.prims: list[tuple] = []

    def poly(self, pts, fill):
        self.prims.append(("poly", pts, fill))

    def line(self, x1, y1, x2, y2, width, color):
        self.prims.append(("line", x1, y1, x2, y2, width, color))

    def circle(self, x, y, r, fill):
        self.prims.append(("circle", x, y, r, fill))

    def text(self, x, y, s, size, color):
        self.prims.append(("text", x, y, s, size, color))

    def _px(self, x, y):
        return ((x - self.x0) * self.scale, (y - self.y0) * self.scale)

    def save(self, stem: Path) -> Path:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return self._svg(stem.parent / (stem.name + ".svg"))
        img = Image.new("RGBA", (int(self.w * self.scale), int(self.h * self.scale)), BG)
        d = ImageDraw.Draw(img, "RGBA")
        fonts: dict[int, object] = {}

        def font(px: int):
            if px not in fonts:
                try:
                    fonts[px] = ImageFont.load_default(size=px)
                except TypeError:        # Pillow < 10.1: one fixed bitmap size
                    fonts[px] = ImageFont.load_default()
            return fonts[px]

        for p in self.prims:
            if p[0] == "poly" and len(p[1]) >= 3:
                d.polygon([self._px(x, y) for x, y in p[1]], fill=p[2])
            elif p[0] == "line":
                _, x1, y1, x2, y2, w, c = p
                d.line([self._px(x1, y1), self._px(x2, y2)], fill=c,
                       width=max(1, int(round(w * self.scale))))
            elif p[0] == "circle":
                _, x, y, r, c = p
                cx, cy = self._px(x, y)
                rr = max(1.0, r * self.scale)
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=c)
            elif p[0] == "text":
                _, x, y, s, size, c = p
                px = max(8, int(size * self.scale))
                d.text(self._px(x, y), s, fill=c, font=font(px), anchor="mm")
        out = stem.parent / (stem.name + ".png")
        img.convert("RGB").save(out)
        return out

    def _svg(self, out: Path) -> Path:
        def rgba(c):
            return f"rgba({c[0]},{c[1]},{c[2]},{c[3] / 255:.2f})"

        W, H = self.w * self.scale, self.h * self.scale
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}">',
                 f'<rect width="100%" height="100%" fill="{rgba(BG)}"/>']
        for p in self.prims:
            if p[0] == "poly" and len(p[1]) >= 3:
                pts = " ".join("%.2f,%.2f" % self._px(x, y) for x, y in p[1])
                parts.append(f'<polygon points="{pts}" fill="{rgba(p[2])}"/>')
            elif p[0] == "line":
                _, x1, y1, x2, y2, w, c = p
                (a, b), (e, f) = self._px(x1, y1), self._px(x2, y2)
                parts.append(f'<line x1="{a:.2f}" y1="{b:.2f}" x2="{e:.2f}" y2="{f:.2f}" '
                             f'stroke="{rgba(c)}" stroke-width="{max(1, w * self.scale):.2f}" '
                             f'stroke-linecap="round"/>')
            elif p[0] == "circle":
                _, x, y, r, c = p
                cx, cy = self._px(x, y)
                parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r * self.scale:.2f}" '
                             f'fill="{rgba(c)}"/>')
            elif p[0] == "text":
                _, x, y, s, size, c = p
                cx, cy = self._px(x, y)
                s = s.replace("&", "&amp;").replace("<", "&lt;")
                parts.append(f'<text x="{cx:.2f}" y="{cy:.2f}" fill="{rgba(c)}" '
                             f'font-size="{max(8, size * self.scale):.1f}" font-family="sans-serif" '
                             f'text-anchor="middle" dominant-baseline="middle">{s}</text>')
        parts.append("</svg>")
        out.write_text("\n".join(parts), encoding="utf-8")
        return out


def poly_points(ps, pcbnew) -> list[list[tuple[float, float]]]:
    out = []
    for i in range(ps.OutlineCount()):
        ol = ps.Outline(i)
        out.append([(pcbnew.ToMM(ol.CPoint(k).x), pcbnew.ToMM(ol.CPoint(k).y))
                    for k in range(ol.PointCount())])
    return out


def mst_links(islands: list[list[tuple]]) -> list[tuple]:
    """Prim over islands; each link joins the two closest pads of two islands."""
    if len(islands) < 2:
        return []
    joined = {0}
    links = []
    while len(joined) < len(islands):
        best = None
        for i in joined:
            for j in range(len(islands)):
                if j in joined:
                    continue
                for a in islands[i]:
                    for b in islands[j]:
                        dd = (a[2] - b[2]) ** 2 + (a[3] - b[3]) ** 2
                        if best is None or dd < best[0]:
                            best = (dd, j, a, b)
        joined.add(best[1])
        links.append((best[2], best[3]))
    return links


def main() -> int:
    ap = _lib.pass_parser(PASS)
    ap.add_argument("--out", default=None, help="output dir (default: review/ beside the board)")
    ap.add_argument("--route-report", default=None,
                    help="route.py's JSON line, saved (route.py --report)")
    ap.add_argument("--scale", type=float, default=20.0, help="pixels per mm")
    args = ap.parse_args()

    import pcbnew

    board = Path(args.board)
    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew could not load {board} (KiCad major mismatch?)")
    out_dir = Path(args.out) if args.out else board.parent / "review"
    out_dir.mkdir(parents=True, exist_ok=True)

    finished: set[str] = set()
    router_left = 0
    if args.route_report and not Path(args.route_report).exists():
        # the Makefile always passes it; before a route there is none
        print(f"  no route report at {args.route_report}: routing "
              "metrics unavailable", file=sys.stderr)
        args.route_report = None
    if args.route_report:
        rep = json.loads(Path(args.route_report).read_text(encoding="utf-8"))
        router_left = (rep.get("unrouted_after_router") or {}).get("pad_links_missing", 0)
        finished = set((rep.get("unrouted_after_router") or {}).get("nets", {}))

    now = _lib.unrouted_nets(board, clusters=True)
    finished -= set(now["nets"])          # still broken: red, not orange
    stats = _lib.track_stats(b)

    bb = b.GetBoardEdgesBoundingBox()
    box = (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
           pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
    outline = pcbnew.SHAPE_POLY_SET()
    b.GetBoardPolygonOutlines(outline, True)
    outline_pts = poly_points(outline, pcbnew)
    cu = [(lid, b.GetLayerName(lid)) for lid in b.GetEnabledLayers().CuStack()]

    links = [(net, a, c) for net, isl in now["clusters"].items() for a, c in mst_links(isl)]

    def draw(canvas: Canvas, layers: list[tuple[int, str]], title: str) -> None:
        for pts in outline_pts:
            for k in range(len(pts)):
                x1, y1 = pts[k]
                x2, y2 = pts[(k + 1) % len(pts)]
                canvas.line(x1, y1, x2, y2, 0.15, OUTLINE)
        # Zone fills only on single-layer panels: stacked, four planes bury
        # everything else on the "all" panel.
        for lid, lname in (layers if len(layers) == 1 else []):
            rgb = LAYER_RGB.get(lname, FALLBACK_RGB)
            for z in b.Zones():
                if z.GetIsRuleArea() or not z.IsOnLayer(lid):
                    continue
                for pts in poly_points(z.GetFilledPolysList(lid), pcbnew):
                    canvas.poly(pts, (*rgb, 55))
        for lid, lname in layers:
            rgb = LAYER_RGB.get(lname, FALLBACK_RGB)
            for t in b.GetTracks():
                if isinstance(t, pcbnew.PCB_VIA) or t.GetLayer() != lid:
                    continue
                s, e = t.GetStart(), t.GetEnd()
                sx, sy = pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)
                col = (*rgb, 230)
                if t.GetNetname() in finished:
                    col = FINISHED
                if _lib.off_angle(pcbnew.ToMM(e.x) - sx, pcbnew.ToMM(e.y) - sy)                         and not isinstance(t, pcbnew.PCB_ARC):
                    col = OFF_ANGLE
                canvas.line(sx, sy, pcbnew.ToMM(e.x), pcbnew.ToMM(e.y),
                            pcbnew.ToMM(t.GetWidth()), col)
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                for lid, lname in layers:
                    if not pad.IsOnLayer(lid):
                        continue
                    rgb = LAYER_RGB.get(lname, FALLBACK_RGB)
                    ps = pcbnew.SHAPE_POLY_SET()
                    pad.TransformShapeToPolygon(ps, lid, 0, pcbnew.FromMM(0.01),
                                                pcbnew.ERROR_INSIDE)
                    for pts in poly_points(ps, pcbnew):
                        canvas.poly(pts, (min(255, rgb[0] + 40), min(255, rgb[1] + 40),
                                          min(255, rgb[2] + 40), 255))
                if pad.HasHole():
                    p = pad.GetPosition()
                    canvas.circle(pcbnew.ToMM(p.x), pcbnew.ToMM(p.y),
                                  pcbnew.ToMM(pad.GetDrillSize().x) / 2, HOLE)
        for t in b.GetTracks():
            if isinstance(t, pcbnew.PCB_VIA):
                p = t.GetPosition()
                x, y = pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)
                canvas.circle(x, y, pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu)) / 2, VIA)
                canvas.circle(x, y, pcbnew.ToMM(t.GetDrillValue()) / 2, HOLE)
        for fp in b.GetFootprints():
            p = fp.GetPosition()
            canvas.text(pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), fp.GetReference(), 0.9, TEXT)
        for net, a, c in links:
            canvas.line(a[2], a[3], c[2], c[3], 0.12, RAT)
            canvas.text((a[2] + c[2]) / 2, (a[3] + c[3]) / 2 - 0.6, net, 0.7, RAT)
        head, _, rest = title.partition(" -- red:")
        mid = box[0] + (box[2] - box[0]) / 2
        canvas.text(mid, box[1] - 4.4, head, 1.4, TEXT)
        canvas.text(mid, box[1] - 2.2, ("red:" + rest) if rest else "", 0.9, TEXT)

    legend = (f"red: {now['pad_links_missing']} link(s) unrouted in {len(now['nets'])} net(s)"
              + (f" | orange: {len(finished)} net(s) the router left, completion finished"
                 if args.route_report else "")
              + f" | magenta: {len(stats['off_angle_segments'])} off-45 seg")
    written = []
    panels = [("all", cu)] + [(name, [(lid, name)]) for lid, name in cu]
    for pname, layers in panels:
        canvas = Canvas(box, args.scale)
        draw(canvas, layers, f"{board.stem} -- {pname} -- {legend}")
        # "-" not ".": Path.with_suffix would eat ".F_Cu" as an extension
        written.append(str(canvas.save(out_dir / f"{board.stem}-{pname.replace('.', '_')}")))
    # KiCad's own plot of each copper layer, beside ours: an independent
    # rendering of the same board. If a pad looks rotated in one and not the
    # other, one of the two renderers is wrong, and that is worth knowing.
    import os
    import shlex
    import shutil
    import subprocess
    cli = shlex.split(os.environ.get("KICAD_CLI", "kicad-cli"))
    kicad_dir = out_dir / "kicad"
    if shutil.which(cli[0]):
        kicad_dir.mkdir(exist_ok=True)
        proc = subprocess.run(
            [*cli, "pcb", "export", "svg", "--mode-multi",
             "--layers", ",".join(name for _lid, name in cu), "--cl", "Edge.Cuts",
             "--fit-page-to-board", "--exclude-drawing-sheet", "--drill-shape-opt", "1",
             "-o", f"{kicad_dir}/", str(board)], capture_output=True, text=True)
        if proc.returncode == 0:
            written += [str(p) for p in sorted(kicad_dir.glob(f"{board.stem}-*.svg"))]
        else:
            print(f"  kicad-cli svg failed ({proc.returncode}): {proc.stderr[:200]}",
                  file=sys.stderr)
    else:
        print(f"  {cli[0]} not found: no KiCad plots, only ours", file=sys.stderr)
    for w in written:
        print(f"  wrote {w}", file=sys.stderr)

    _lib.emit(PASS, board=str(board), out=[str(w) for w in written],
              unrouted_nets=now["nets"], pad_links_missing=now["pad_links_missing"],
              router_left_links=router_left, finished_by_completion=sorted(finished),
              off_angle_segments=len(stats["off_angle_segments"]), vias=stats["vias"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
