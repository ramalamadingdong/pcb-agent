#!/usr/bin/env python3
"""
Restore footprint-local pad coordinates after `kct optimize-placement`.

BUG THIS WORKS AROUND
---------------------
`kct optimize-placement` writes ABSOLUTE board coordinates into each footprint's
FOOTPRINT-LOCAL ``(pad ... (at x y))`` node.  KiCad interprets those as offsets
from the footprint origin, so every pad lands far from where it belongs, and
both pads of a two-pad passive collapse onto the *same* point:

    after create-pcb        C17 pads: [('1', '-0.775', '0'), ('2', '0.775', '0')]   correct
    after optimize-placement C17 pads: [('1', '142.598061', '92.091833'),
                                        ('2', '142.598061', '92.091833')]           corrupt

kicad-tools' own Pad model documents the invariant it breaks: "``position`` is
stored footprint-local and must be rotated by ``footprint.rotation`` to reach
board coordinates".  Assigning an absolute value mirrors straight through
``Pad.__setattr__`` into the S-expression, so ``PCB.save`` writes it out.

Consequences on a board that has been through the optimiser:
  * ~200 DRC `shorting_items`, one per two-pad passive, pads stacked
  * hundreds of bogus `clearance` violations
  * the autorouter cannot escape ANY pad, because the pad geometry it is
    routing to is fiction

This pass rewrites every pad's ``(at ...)`` from the footprint library that pad
came from, which is the authoritative source.  Placement (the footprint's own
``(at ...)``) is untouched -- that part of the optimiser's output is correct.

Run immediately after any `kct optimize-placement` / `kct placement fix` cycle.
`place.py` does exactly that, every round.

Idempotent: every pad is rewritten from the library, so a second run over a
repaired board writes the same bytes and reports 0 repaired footprints.

board.toml schema
-----------------
::

    [project]
    footprint_lib = "myboard.pretty"   # optional, see footprint_lib()

--netlist is accepted for contract uniformity and unused.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import _lib

NAME = "repair_pads"

PAD_AT = re.compile(r'(\(pad\s+"[^"]*"[\s\S]{0,400}?\(at\s+)([-\d.]+\s+[-\d.]+(?:\s+[-\d.]+)?)(\s*\))')


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


def sexp_blocks(text: str, token: str):
    """Yield (start, end) spans of every top-level `(token ...)` block."""
    i = 0
    while True:
        j = text.find(f"({token} ", i)
        if j < 0:
            return
        depth, k = 0, j
        while k < len(text):
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        yield j, k
        i = k


def library_pad_coords(lib: Path, fp_name: str) -> list[str] | None:
    """Ordered list of `(at ...)` payloads for a footprint, from the library."""
    path = lib / f"{fp_name.split(':')[-1]}.kicad_mod"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return [m.group(2) for m in PAD_AT.finditer(text)]


def main() -> int:
    ap = _lib.pass_parser(NAME)
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found")
    cfg = _lib.load_config(args.config)
    lib = footprint_lib(cfg, board)
    if not lib.exists():
        _lib.fail(f"missing footprint library {lib} -- run make_libs first")

    text = board.read_text(encoding="utf-8")

    out: list[str] = []
    cursor = 0
    repaired = fps = skipped = 0

    for start, end in sexp_blocks(text, "footprint"):
        out.append(text[cursor:start])
        block = text[start:end]
        cursor = end
        fps += 1

        name_m = re.match(r'\(footprint\s+"([^"]+)"', block)
        coords = library_pad_coords(lib, name_m.group(1)) if name_m else None
        if not coords:
            skipped += 1
            out.append(block)
            continue

        idx = 0
        changed = 0

        # Pad angles in the board file are stored as footprint rotation + pad-local
        # rotation.  The library is the unrotated frame, so its angle term must be
        # offset by this footprint's own rotation or a rotated footprint's pads
        # come out axis-aligned (adjacent pads on a rotated fine-pitch connector
        # short at 1.1 mm pitch).
        fp_rot = 0.0
        rot_m = re.search(
            r'\(footprint\s+"[^"]+"[\s\S]{0,600}?\n\t+\(at\s+'
            r'[-\d.]+\s+[-\d.]+(?:\s+([-\d.]+))?\)',
            block,
        )
        if rot_m and rot_m.group(1):
            fp_rot = float(rot_m.group(1))

        def sub(m: re.Match) -> str:
            nonlocal idx, changed
            if idx >= len(coords):
                return m.group(0)
            want = coords[idx]
            idx += 1
            parts = want.split()
            lib_ang = float(parts[2]) if len(parts) > 2 else 0.0
            ang = (lib_ang + fp_rot) % 360
            want_out = f"{parts[0]} {parts[1]}" + (f" {ang:g}" if ang else "")
            if m.group(2).split() != want_out.split():
                changed += 1
            return m.group(1) + want_out + m.group(3)

        block = PAD_AT.sub(sub, block)
        if changed:
            repaired += 1
        out.append(block)

    out.append(text[cursor:])
    # newline="" so the write cannot re-encode line endings. Without it a
    # Windows run turns every LF in the board into CRLF while the mounting-hole
    # pass turns them back, and neither is ever byte-stable.
    board.write_text("".join(out), encoding="utf-8", newline="")
    nets = _lib.assert_net_table(board)

    print(
        f"Pad repair: {repaired}/{fps} footprints had corrupt pad coordinates restored",
        file=sys.stderr,
    )
    if skipped:
        print(f"  ({skipped} footprint(s) not found in {lib.name}, left untouched)", file=sys.stderr)

    _lib.emit(
        NAME,
        board=str(board),
        footprints=fps,
        repaired=repaired,
        skipped=skipped,
        library=str(lib),
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
