#!/usr/bin/env python3
"""Inject the mounting holes declared in board.toml.

board.toml always specified the holes, but nothing in the pipeline ever
created them — two revisions shipped with no way to bolt the board into an
enclosure.  Like the fiducials, holes are not netlist objects, so they are
injected as footprints from the vendored footprint library.

Unlike the fiducials this MUST run BEFORE the placement loop: the screw-head
courtyard is an obstacle the optimiser has to pack around, and the pour
clearance ring changes the copper.  The holes are injected `(locked yes)` and
their refs belong in the placement loop's anchor list (`kct placement fix
--strategy anchor` moves ANY part absent from that list, locked or not) —
`place.py` builds that list from the same board.toml section, via the
``hole_refs()`` helper below, so the two cannot drift.

Positions are BOARD-FRAME millimetres: origin at the board's BOTTOM-LEFT
corner of the Edge.Cuts outline, X right, Y UP — the same frame every other
coordinate in board.toml uses, and the frame ``scripts/validate_gerbers.py``
checks.  KiCad pages run Y DOWN, so they are converted on the way in; see
``_lib.board_frame``.  Keep them clear of any antenna keepout band: a steel
screw inside the band detunes the module antenna, which is exactly what the
band exists to prevent.

Idempotent: previously injected holes are stripped before adding, and every
value written (position, ref, uuid) is derived from board.toml, so a re-run
reproduces the file byte for byte.  This pass splices a complete block copied
from a library `.kicad_mod`; it never does coordinate arithmetic on existing
board geometry.

board.toml schema
-----------------
::

    [mounting_holes]
    count        = 4        # cross-checked against positions; the gerber
                            # checker counts holes in the drill file
    diameter_mm  = 3.2
    tolerance_mm = 0.05     # gerber checker only, unused here
    positions    = [[5.0, 12.0], [50.0, 12.0], [5.0, 85.0], [50.0, 85.0]]
                            # board-frame mm (bottom-left origin, Y up)
    ref_prefix   = "H"                  # optional, default "H" -> H1..Hn
    footprint    = "MountingHole_3.2mm" # optional, default
                                        # MountingHole_<diameter_mm>mm

    [project]
    footprint_lib = "myboard.pretty"    # optional, see footprint_lib()

No [mounting_holes] section at all means "this board has none": the pass
reports 0 placed and exits 0.  A section that declares holes but no
`positions` is a failure — the drill-file check downstream would fail anyway,
and it fails here with something you can act on.

Not ported: the source's per-hole rationale comments (which parts each hole
had to clear) are board-specific — keep them next to the coordinates in
board.toml.

--netlist is accepted for contract uniformity and unused.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import _lib

NAME = "add_mounting_holes"


def footprint_lib(cfg: dict, board: Path) -> Path:
    """The vendored footprint library for this project.

    `[project] footprint_lib` in board.toml; a relative path resolves against
    the board file's directory.  Default: the only `*.pretty` directory beside
    the board — a project that vendors more than one has to name it.
    """
    raw = (cfg.get("project") or {}).get("footprint_lib")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (board.parent / p)
    found = sorted(q for q in board.parent.glob("*.pretty") if q.is_dir())
    if len(found) == 1:
        return found[0]
    if not found:
        _lib.fail(
            f"no *.pretty footprint library beside {board} — "
            "run make_libs first, or set [project] footprint_lib in board.toml"
        )
    _lib.fail(
        f"{len(found)} *.pretty libraries beside {board} "
        f"({', '.join(q.name for q in found)}) — name one in [project] footprint_lib"
    )


def hole_footprint_name(cfg: dict) -> str:
    mh = cfg.get("mounting_holes") or {}
    name = mh.get("footprint")
    if name:
        return str(name)
    dia = mh.get("diameter_mm")
    if dia is None:
        _lib.fail("[mounting_holes] needs diameter_mm (or an explicit footprint = ...)")
    return f"MountingHole_{float(dia):g}mm"


def hole_refs(cfg: dict) -> list[str]:
    """Refs this pass will create, in order.  Imported by place.py so the
    anchor list and the injected holes cannot drift apart."""
    mh = cfg.get("mounting_holes") or {}
    prefix = str(mh.get("ref_prefix", "H"))
    n = len(mh.get("positions") or [])
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def sites(cfg: dict) -> list[tuple[str, float, float]]:
    """(ref, board-frame x, board-frame y) for each declared hole.

    Board frame = origin at the outline's bottom-left corner, Y UP; the caller
    converts to KiCad page mm.  See ``_lib.board_frame``.
    """
    mh = cfg.get("mounting_holes") or {}
    positions = mh.get("positions")
    if positions is None:
        _lib.fail(
            "[mounting_holes] declares holes but no positions — add "
            "positions = [[x, y], ...] in board-frame mm. "
            "Nothing else in the pipeline emits mounting holes."
        )
    out: list[tuple[str, float, float]] = []
    for ref, p in zip(hole_refs(cfg), positions):
        if len(p) != 2:
            _lib.fail(f"[mounting_holes] positions entry for {ref} must be [x, y], got {p!r}")
        out.append((ref, float(p[0]), float(p[1])))
    want = mh.get("count")
    if want is not None and int(want) != len(out):
        _lib.fail(
            f"[mounting_holes] count = {int(want)} but positions lists {len(out)} — "
            "the gerber checker counts holes in the drill file and would fail on the mismatch"
        )
    return out


def board_bbox(pcb_text: str) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for m in re.finditer(
        r'\(gr_line\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)'
        r'(?:(?!\)\s*\(gr_|\Z).)*?\(layer "Edge\.Cuts"\)',
        pcb_text,
        re.S,
    ):
        x1, y1, x2, y2 = (float(g) for g in m.groups())
        xs += [x1, x2]
        ys += [y1, y2]
    if not xs:
        _lib.fail("no Edge.Cuts outline found -- cannot place mounting holes")
    return min(xs), min(ys), max(xs), max(ys)


def strip_existing(pcb_text: str, fp_name: str) -> tuple[str, int]:
    out, removed = [], 0
    i = 0
    needle = f'\t(footprint "{fp_name}"'
    while True:
        j = pcb_text.find(needle, i)
        if j < 0:
            out.append(pcb_text[i:])
            break
        out.append(pcb_text[i:j])
        depth, k = 0, j
        while k < len(pcb_text):
            if pcb_text[k] == "(":
                depth += 1
            elif pcb_text[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        while k < len(pcb_text) and pcb_text[k] in "\r\n":
            k += 1
        i = k
        removed += 1
    return "".join(out), removed


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found — run create-pcb first")
    cfg = _lib.load_config(args.config)

    if not cfg.get("mounting_holes"):
        print("no [mounting_holes] section in board.toml — nothing to place", file=sys.stderr)
        _lib.emit(NAME, board=str(board), placed=0, stripped=0, skipped="no [mounting_holes] section")
        return 0

    fp_name = hole_footprint_name(cfg)
    lib = footprint_lib(cfg, board)
    fp_file = lib / f"{fp_name}.kicad_mod"
    if not fp_file.exists():
        _lib.fail(f"missing {fp_file} -- run make_libs first")

    site_list = sites(cfg)
    board_name = str((cfg.get("board") or {}).get("name") or board.stem)

    text = board.read_text(encoding="utf-8")
    text, removed = strip_existing(text, fp_name)
    if removed:
        print(f"  stripped {removed} existing mounting hole(s)", file=sys.stderr)

    min_x, _, _, max_y = board_bbox(text)
    # config is board-frame (bottom-left, Y-up) — see _lib.board_frame.  This
    # pass has no pcbnew board object, so the frame comes from the same
    # Edge.Cuts scan above: bottom-left is (min x, MAX y) on a Y-down page.
    frame = (min_x, max_y)

    mod = fp_file.read_text(encoding="utf-8").rstrip()
    blocks = []
    for ref, bx, by in site_list:
        x, y = _lib.to_kicad_xy(frame, bx, by)
        b = mod.replace(
            '(layer "F.Cu")',
            f'(locked yes)\n\t(layer "F.Cu")\n\t(uuid "{uuid.uuid5(uuid.NAMESPACE_DNS, f"{board_name}-{ref}")}")'
            f"\n\t(at {x} {y})",
            1,
        )
        b = b.replace('(property "Reference" "REF**"', f'(property "Reference" "{ref}"', 1)
        blocks.append("\n".join("\t" + ln if ln.strip() else ln for ln in b.splitlines()))

    stripped = text.rstrip()
    if not stripped.endswith(")"):
        _lib.fail("unexpected board file tail -- refusing to edit")
    new = stripped[: stripped.rfind(")")] + "\n".join(blocks) + "\n)\n"
    board.write_text(new, encoding="utf-8", newline="")
    nets = _lib.assert_net_table(board)

    for ref, bx, by in site_list:
        kx, ky = _lib.to_kicad_xy(frame, bx, by)
        print(
            f"  {ref}: board-frame ({bx:.1f}, {by:.1f})  KiCad ({kx:.1f}, {ky:.1f})",
            file=sys.stderr,
        )
    dia = (cfg.get("mounting_holes") or {}).get("diameter_mm")
    print(
        f"Placed {len(site_list)} mounting holes "
        f"(Ø{dia} mm NPTH, locked; copper clearance comes from {fp_name})",
        file=sys.stderr,
    )

    _lib.emit(
        NAME,
        board=str(board),
        placed=len(site_list),
        stripped=removed,
        footprint=fp_name,
        refs=[r for r, _, _ in site_list],
        # The board-frame origin in KiCad page mm: the outline's bottom-left.
        origin_mm=[round(frame[0], 3), round(frame[1], 3)],
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
