#!/usr/bin/env python3
"""Tests for link_schematic.py and check_parity.py.

    python3 scripts/build/test_parity.py [board_dir]

Runs against a copy of the example board (or board_dir), never the original.
It links the copy, requires the gate to pass, then damages a fresh linked
copy once per case, the way a post-route pass could, and requires the gate
to FAIL each time. A gate that has only ever been seen passing hasn't been
tested.

Needs pcbnew + kicad-cli of the board's KiCad major (the container has
both); without them every case reports SKIP, and a SKIP is not a PASS.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1] / "examples" / "unoq-power-shield"

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n      {detail}"))
    if not ok:
        failures += 1


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def run(script: str, board: Path, *extra: str) -> tuple[int, dict, str]:
    cmd = [sys.executable, str(HERE / script), "--board", str(board),
           "--config", str(board.parent / "board.toml"), *extra]
    p = subprocess.run(cmd, capture_output=True, text=True)
    js: dict = {}
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            js = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return p.returncode, js, p.stderr


def gate(board: Path) -> tuple[int, dict, str]:
    return run("check_parity.py", board, "--drc-runs", "1")


def mutate(board: Path, fn) -> None:
    import pcbnew
    b = pcbnew.LoadBoard(str(board))
    fn(pcbnew, b)
    pcbnew.SaveBoard(str(board), b)


# ---- the damage a pass could do -------------------------------------------

def pad_to_other_net(pcbnew, b):
    """A pass re-nets a pad (e.g. a stitching fix that 'fixes' the wrong pad)."""
    fp = b.FindFootprintByReference("U1")
    pad = next(p for p in fp.Pads() if p.GetNumber() == "3")
    pad.SetNet(b.FindNet("GND"))


def stray_part(pcbnew, b):
    """A pass adds a footprint the schematic never had."""
    src = b.FindFootprintByReference("R2")
    dup = pcbnew.Cast_to_FOOTPRINT(src.Duplicate(False))
    dup.SetReference("R99")
    dup.SetPath(pcbnew.KIID_PATH("/00000000-0000-0000-0000-000000000099"))
    dup.Move(pcbnew.VECTOR2I(pcbnew.FromMM(3), 0))
    b.Add(dup)


def short_track(pcbnew, b):
    """A pass lays copper labelled with one net across a pad of another."""
    fp = b.FindFootprintByReference("U1")
    a = next(p for p in fp.Pads() if p.GetNumber() == "3")
    c = next(p for p in fp.Pads() if p.GetNumber() == "2")
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(a.GetPosition())
    t.SetEnd(c.GetPosition())
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(a.GetNet())
    b.Add(t)


def hide_part(pcbnew, b):
    """A pass marks a circuit part 'Not in schematic' to silence parity."""
    fp = b.FindFootprintByReference("R5")
    fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY)


def netted_hole(pcbnew, b):
    """A pass ties a board-only mounting hole's pad to a net."""
    fp = b.FindFootprintByReference("H1")
    for p in fp.Pads():
        p.SetNet(b.FindNet("GND"))


def value_change(pcbnew, b):
    b.FindFootprintByReference("R2").SetValue("4k7-hand-fixed")


def unlink(pcbnew, b):
    b.FindFootprintByReference("R2").SetPath(pcbnew.KIID_PATH(""))


def drop_part(pcbnew, b):
    b.Remove(b.FindFootprintByReference("R2"))


CASES = [
    ("pad moved to another net", pad_to_other_net, "independent_diff"),
    ("stray footprint added", stray_part, "independent_diff"),
    ("copper shorts two nets", short_track, "shorts"),
    ("circuit part marked board-only", hide_part, "independent_diff"),
    ("mechanical hole given a net", netted_hole, "independent_diff"),
    ("value changed on the board", value_change, "independent_diff"),
    ("symbol link lost", unlink, "independent_diff"),
    ("footprint removed", drop_part, "independent_diff"),
]


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else EXAMPLE
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        print("SKIP  all -- no pcbnew (KiCad 9/10) on this interpreter")
        return 0
    if shutil.which("kicad-cli") is None:
        print("SKIP  all -- no kicad-cli")
        return 0

    with tempfile.TemporaryDirectory() as t:
        work = Path(t) / "board"
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("fab", "build", "release"))
        board = next(p for p in work.glob("*.kicad_pcb") if p.name != "pre_route.kicad_pcb")

        rc, js, err = run("link_schematic.py", board)
        check("link: pass succeeds", rc == 0, err[-400:])
        h = md5(board)
        rc, js, err = run("link_schematic.py", board)
        check("link: re-run changes no byte", rc == 0 and md5(board) == h and not js.get("wrote"),
              err[-300:])
        rc, js, err = gate(board)
        check("gate: linked board passes", rc == 0 and js.get("ok") is True, err[-1200:])
        golden = board.read_bytes()

        for name, fn, field in CASES:
            board.write_bytes(golden)
            mutate(board, fn)
            rc, js, err = gate(board)
            check(f"gate FAILS: {name}", rc != 0 and (js.get(field) or 0) > 0,
                  f"rc={rc} {field}={js.get(field)} {err[-400:]}")

        # link must refuse to paper over a stray part, not link it
        board.write_bytes(golden)
        mutate(board, stray_part)
        rc, _, err = run("link_schematic.py", board)
        check("link refuses a footprint with no symbol", rc != 0 and "R99" in err, err[-300:])

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
