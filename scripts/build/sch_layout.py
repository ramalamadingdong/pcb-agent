"""Schematic layout geometry: boxes, label extents and a skyline packer.

Pure arithmetic, no KiCad import, so it is testable anywhere and
generate_schematic can lean on it without a second source of truth.

Everything is schematic millimetres, Y DOWN (the page frame). Every offset
the packer hands back is a whole number of 2.54 mm grid steps: a symbol
moved off the connection grid still *draws* fine, and its labels then sit a
fraction of a millimetre off its pins and carry no net. The round-trip diff
would catch that, but the packer should never be the reason it has to.
"""

from __future__ import annotations

import math

GRID = 2.54

Box = tuple[float, float, float, float]  # x0, y0, x1, y1

# KiCad draws schematic text at 1.27 mm. Its stroke font averages under
# 0.9 mm per glyph at that size; 1.0 plus the arrow and padding of a global
# label keeps the estimate conservative -- an overestimate costs a little
# paper, an underestimate draws a label through a neighbour.
CHAR_W = 1.0
LABEL_PAD = 3.2
TEXT_H = 2.6

# Direction a pin points away from its symbol body, as a label rotation.
DIRS = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}


def snap_up(v: float) -> float:
    """Round a length UP to whole grid steps."""
    return math.ceil(round(v / GRID, 6)) * GRID


def r2(v: float) -> float:
    return round(v, 2)


def union(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def shift(b: Box, dx: float, dy: float) -> Box:
    return (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)


def grow(b: Box, m: float) -> Box:
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)


def hits(a: Box, b: Box) -> bool:
    """Strict overlap: boxes that only share an edge do not collide."""
    return a[0] < b[2] - 1e-6 and b[0] < a[2] - 1e-6 and a[1] < b[3] - 1e-6 and b[1] < a[3] - 1e-6


def label_box(x: float, y: float, direction: tuple[int, int], text: str) -> Box:
    """Extent of a global label anchored at (x, y), body pointing `direction`."""
    length = CHAR_W * len(text) + LABEL_PAD
    h = TEXT_H / 2
    dx, dy = direction
    if dx:
        return (x, y - h, x + length, y + h) if dx > 0 else (x - length, y - h, x, y + h)
    return (x - h, y, x + h, y + length) if dy > 0 else (x - h, y - length, x + h, y)


def text_box(cx: float, cy: float, text: str) -> Box:
    # Field text is not always drawn centred on its anchor; allow a glyph
    # of slack each side.
    w = CHAR_W * len(text) / 2 + 1.27
    return (cx - w, cy - TEXT_H / 2, cx + w, cy + TEXT_H / 2)


def wire_box(a: tuple[float, float], b: tuple[float, float], half: float = 0.5) -> Box:
    return (min(a[0], b[0]) - half, min(a[1], b[1]) - half,
            max(a[0], b[0]) + half, max(a[1], b[1]) + half)


def skyline_pack(items: list[tuple[str, float, float]], width: float, gap: float = GRID * 2):
    """Place (key, w, h) blocks top-down, "tetris" style, inside `width`.

    Bottom-left skyline: the skyline is the lowest occupied y over each x
    span. Every block goes where its top edge would be highest (smallest
    y), leftmost on a tie, and the skyline under it drops to its bottom.
    Blocks are taken in the order given -- the caller decides priority
    (tallest first packs tightest). A block wider than `width` still goes
    in, alone at the left, rather than failing: a sheet that is too narrow
    is a paper-size problem, not a reason to lose a part.

    Returns {key: (x, y)} top-left offsets, whole grid steps, from (0, 0).
    """
    width = max(width, max((snap_up(w + gap) for _, w, _ in items), default=0))
    sky: list[list[float]] = [[0.0, width, 0.0]]  # [x, w, y]
    out: dict[str, tuple[float, float]] = {}
    for key, w, h in items:
        w, h = snap_up(w + gap), snap_up(h + gap)
        best = None
        for i in range(len(sky)):
            x = sky[i][0]
            if x + w > width + 1e-6:
                break
            top, span, j = 0.0, 0.0, i
            while span < w - 1e-6:
                top = max(top, sky[j][2])
                span += sky[j][1]
                j += 1
            if best is None or (top, x) < (best[0], best[1]):
                best = (top, x)
        top, x = best
        out[key] = (r2(x), r2(top))
        # Replace the covered span with one segment at the block's bottom.
        new: list[list[float]] = []
        for sx, sw, sy in sky:
            end = sx + sw
            if end <= x + 1e-6 or sx >= x + w - 1e-6:
                new.append([sx, sw, sy])
                continue
            if sx < x - 1e-6:
                new.append([sx, x - sx, sy])
            if end > x + w + 1e-6:
                new.append([x + w, end - (x + w), sy])
        new.append([x, w, top + h])
        new.sort()
        merged: list[list[float]] = []
        for seg in new:
            if merged and abs(merged[-1][2] - seg[2]) < 1e-6 and abs(merged[-1][0] + merged[-1][1] - seg[0]) < 1e-6:
                merged[-1][1] += seg[1]
            else:
                merged.append(seg)
        sky = merged
    return out
