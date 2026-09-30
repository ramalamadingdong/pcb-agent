#!/usr/bin/env python3
"""Force every pad's orientation to footprint rotation + library-local angle.

Somewhere in the placement round-trip (kct optimize-placement -> repair_pads
-> tuck_in) rotated footprints lose their pad ANGLE terms: on the first board
with a micro-SD socket at rot 90, all contact pads came out angle 0 —
0.6 x 1.3 mm pads lying lengthwise along a 1.1 mm pitch, overlapping their
neighbours (9 shorting_items + 19 solder-mask bridges on that one part).  This
is the same failure family repair_pads exists for, surviving somewhere its
regex pass cannot reach.

Rather than chase the serializer, this pcbnew pass enforces the invariant
directly: pad.orientation = footprint.orientation + library_local_angle.
Library-local angles are read from the vendored footprint library by matching
pads on FOOTPRINT-LOCAL position (pad numbers repeat: shield pegs share a
number, unnumbered mechanical pads share the empty one).

Run right after the placement loop, before fanout.  `place.py` does that.
It runs AGAIN in `make build` just before silk_finish, as insurance. The
example board as committed (built before 2026-09-30) has 16 pads on R3-R7,
C1, C6, C7 at 0 deg on footprints rotated 90/270, in pre_route.kicad_pcb and
the routed board alike. A rebuild on 2026-09-30 in the pinned container came
out correct, and the second run found nothing to fix, so the pass that
stripped them was not reproduced. A second run costs a second; that fault
reaching the fab costs a board.
Idempotent: the target angle is computed from the library each time, so a
second run finds every pad already correct and does not rewrite the board.

``--check`` writes nothing: it lists every wrong pad and exits 1. `make
check` runs it, so a pass that reintroduces the fault fails a gate
instead of reaching the fab.

Needs `pcbnew`, which on a host KiCad install imports only under KiCad's own
bundled Python.  Run it with that interpreter (`place.py` honours $KICAD_PYTHON
for exactly this reason); inside the container the default python3 has it.

board.toml schema
-----------------
::

    [project]
    footprint_lib = "myboard.pretty"   # optional, see footprint_lib()

--netlist is accepted for contract uniformity and unused.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import _lib
import pcbnew

NAME = "fix_pad_angles"

PAD_RE = re.compile(
    r'\(pad\s+(?:"[^"]*"|\S+)\s+\S+\s+\S+\s*'
    r'\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)'
)


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


def lib_pads(lib: Path, fp_name: str):
    path = lib / f"{fp_name.split(':')[-1]}.kicad_mod"
    if not path.exists():
        return None
    out = []
    for m in PAD_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        out.append((float(m.group(1)), float(m.group(2)),
                    float(m.group(3)) if m.group(3) else 0.0))
    return out


def wrong_pads(b, lib: Path) -> list[tuple]:
    """(footprint, pad, want_deg, have_deg) for every pad off its library angle."""
    out = []
    for fp in b.GetFootprints():
        name = fp.GetFPID().GetLibItemName().wx_str() if hasattr(
            fp.GetFPID().GetLibItemName(), "wx_str") else str(fp.GetFPID().GetLibItemName())
        lp = lib_pads(lib, name)
        fp_rot = fp.GetOrientation().AsDegrees()
        for pad in fp.Pads():
            rel = pad.GetFPRelativePosition()
            lx, ly = pcbnew.ToMM(rel.x), pcbnew.ToMM(rel.y)
            local_ang = 0.0
            if lp:
                best = min(lp, key=lambda q: (q[0] - lx) ** 2 + (q[1] - ly) ** 2)
                if math.hypot(best[0] - lx, best[1] - ly) < 0.1:
                    local_ang = best[2]
            want = (fp_rot + local_ang) % 360
            have = pad.GetOrientation().AsDegrees() % 360
            if abs((want - have + 180) % 360 - 180) > 0.01:
                out.append((fp, pad, want, have))
    return out


def main() -> int:
    ap = _lib.pass_parser(NAME)
    ap.add_argument("--check", action="store_true",
                    help="report wrong pads and exit 1; write nothing")
    args = ap.parse_args()

    pcb_path = Path(args.board)
    if not pcb_path.exists():
        _lib.fail(f"{pcb_path} not found")
    cfg = _lib.load_config(args.config)
    lib = footprint_lib(cfg, pcb_path)
    if not lib.exists():
        _lib.fail(f"missing footprint library {lib} -- run make_libs first")

    b = pcbnew.LoadBoard(str(pcb_path))
    if b is None:
        # A KiCad major older than the file's cannot load it: it returns None
        # rather than raising, so check.
        _lib.fail(
            f"pcbnew ({pcbnew.GetBuildVersion()}) could not load {pcb_path} — "
            "usually a KiCad older than the file that wrote it"
        )
    wrong = wrong_pads(b, lib)
    refs = sorted({fp.GetReference() for fp, _p, _w, _h in wrong})

    if args.check:
        for fp, pad, want, have in wrong:
            print(f"  FAIL  {fp.GetReference()} pad {pad.GetNumber()}: angle "
                  f"{have:g} deg, library + footprint rotation says {want:g}",
                  file=sys.stderr)
        if not wrong:
            print("  PASS  pad angles: every pad at footprint rotation + "
                  "library angle", file=sys.stderr)
        _lib.emit(NAME, board=str(pcb_path), check=True, pads_wrong=len(wrong),
                  footprints=refs)
        return 1 if wrong else 0

    for _fp, pad, want, _have in wrong:
        pad.SetOrientation(pcbnew.EDA_ANGLE(want, pcbnew.DEGREES_T))
    fixed, fps = len(wrong), len(refs)
    if fixed:
        pcbnew.SaveBoard(str(pcb_path), b)
        print(f"  pad angles: fixed {fixed} pads on {fps} footprints", file=sys.stderr)
    else:
        print("  pad angles: all correct", file=sys.stderr)

    nets = _lib.assert_net_table(pcb_path)
    _lib.emit(NAME, board=str(pcb_path), pads_fixed=fixed, footprints=fps, nets=nets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
