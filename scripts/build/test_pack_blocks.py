#!/usr/bin/env python3
"""Tests for pack_blocks.py.

    python3 scripts/build/test_pack_blocks.py

The search half runs anywhere. The board half needs pcbnew: it builds a
board the way the optimiser leaves one when it cannot reach zero conflicts
-- a free INA226 with a Direct-tagged cap, eight resistors piled on top of
it, a fixed connector, a keepout and a mounting hole -- runs the real
direct_connect and pack_blocks passes, and judges the result with kicad-cli
DRC and pcbnew geometry rather than the pass's own report. Without pcbnew
that half reports SKIP, and a SKIP is not a PASS.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pack_blocks as pb  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail and not ok else ''}")


# ---------------------------------------------------------------- search

def search_tests() -> None:
    sq = lambda s: [(-s / 2, -s / 2, s / 2, s / 2)]  # noqa: E731
    F = frozenset("F")
    space = pb.Space((0, 0, 50, 50), 0.0)
    a = pb.Block("A", ["A"], F, (25, 25), [pb.Variant(0, sq(10))])
    b = pb.Block("B", ["B"], F, (26, 25), [pb.Variant(0, sq(4))])
    plan = pb.pack([a, b], space, 0.25, 3.0, True)
    check("bigger block keeps its spot", plan["A"][1:3] == (25, 25))
    bx = plan["B"][1]
    check("smaller block moves to the nearest free spot, and only that far",
          abs(bx - 32.0) < 1e-6 and abs(plan["B"][2] - 25) < 1e-6, str(plan["B"]))

    space = pb.Space((0, 0, 50, 50), 0.0)
    space.add(F, (0, 0, 50, 20), "wall")
    space.add(F, (0, 22.0, 50, 50), "wall")  # a 2 mm slot along x
    long = pb.Block("L", ["L"], F, (25, 21), [
        pb.Variant(0, [(-1, -4, 1, 4)]), pb.Variant(90, [(-4, -1, 4, 1)])])
    plan = pb.pack([long], space, 0.25, 3.0, True)
    check("a block that only fits turned is rotated", plan["L"][0] == 1, str(plan["L"]))
    plan = pb.pack([pb.Block("L", ["L"], F, (25, 21), long.variants)],
                   pb.Space((0, 0, 50, 50), 0.0), 0.25, 3.0, True)
    check("a block that fits as it is is not rotated", plan["L"][0] == 0 and plan["L"][3] == 0)

    space = pb.Space((0, 0, 50, 50), 0.0)
    space.add(F, (0, 0, 50, 50), "everything")
    try:
        pb.pack([pb.Block("X", ["X"], F, (25, 25), [pb.Variant(0, sq(2))])], space, 0.5, 3.0, True)
        check("a board with no room raises", False)
    except LookupError:
        check("a board with no room raises", True)
    top_bot = pb.Space((0, 0, 50, 50), 0.0)
    top_bot.add(frozenset("B"), (20, 20, 30, 30), "bottom part")
    plan = pb.pack([pb.Block("T", ["T"], F, (25, 25), [pb.Variant(0, sq(4))])], top_bot, 0.25, 3.0, True)
    check("a top part may sit over a bottom part", plan["T"][3] == 0)


# ---------------------------------------------------------------- board

X0, Y0, W, H = 100.0, 50.0, 53.34, 68.58
MSOP10 = {"pads": [(str(n), (-2.1 if n <= 5 else 2.1), (-1 + 0.5 * (n - 1)) if n <= 5 else (1 - 0.5 * (n - 6)),
                    1.5, 0.35) for n in range(1, 11)], "court": (-3.1, -1.77, 3.1, 1.75)}
C0603 = {"pads": [("1", -0.775, 0, 0.9, 0.95), ("2", 0.775, 0, 0.9, 0.95)], "court": (-1.48, -0.73, 1.48, 0.73)}
U2_NETS = {"1": "GND", "2": "GND", "3": "ALERT_3V3", "4": "SDA_3V3", "5": "SCL_3V3",
           "6": "3V3_UNO", "7": "GND", "8": "VIN", "9": "VIN", "10": "VIN_PROT"}
NETLIST = "Net,RefDes,Pin,PinName,Direct,Note\n" + "".join(
    f"{n},U2,{p},x,,\n" for p, n in U2_NETS.items()) + \
    "3V3_UNO,C10,1,~,U2,\nGND,C10,2,~,,\n" + "".join(f"N{i},R{i},1,~,,\nGND,R{i},2,~,,\n" for i in range(1, 9))
CFG = """
[floorplan]
anchors = ["J1"]
[mounting_holes]
positions = [[5.0, 5.0]]
head_diameter_mm = 6.0
[[keepouts]]
name = "antenna"
x1 = 0.0
y1 = 55.0
x2 = 20.0
y2 = 68.58
layers = ["F_Cu", "B_Cu"]
"""


def make_board(path: Path) -> None:
    import pcbnew

    b = pcbnew.BOARD()

    def net(n):
        ni = b.FindNet(n)
        if ni is None:
            ni = pcbnew.NETINFO_ITEM(b, n)
            b.Add(ni)
        return ni

    pts = [(X0, Y0), (X0 + W, Y0), (X0 + W, Y0 + H), (X0, Y0 + H)]
    for i in range(4):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I_MM(*pts[i]))
        s.SetEnd(pcbnew.VECTOR2I_MM(*pts[(i + 1) % 4]))
        b.Add(s)

    def place(ref, spec, x, y, rot, nets):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetReference(ref)
        for num, px, py, sx, sy in spec["pads"]:
            p = pcbnew.PAD(fp)
            p.SetNumber(num)
            p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            p.SetShape(pcbnew.PAD_SHAPE_RECT)
            p.SetSize(pcbnew.VECTOR2I_MM(sx, sy))
            ls = pcbnew.LSET()
            ls.AddLayer(pcbnew.F_Cu)
            ls.AddLayer(pcbnew.F_Mask)
            p.SetLayerSet(ls)
            p.SetPosition(pcbnew.VECTOR2I_MM(px, py))
            fp.Add(p)
        c = pcbnew.PCB_SHAPE(fp)
        c.SetShape(pcbnew.SHAPE_T_RECT)
        c.SetLayer(pcbnew.F_CrtYd)
        c.SetWidth(pcbnew.FromMM(0.05))
        r = spec["court"]
        c.SetStart(pcbnew.VECTOR2I_MM(r[0], r[1]))
        c.SetEnd(pcbnew.VECTOR2I_MM(r[2], r[3]))
        fp.Add(c)
        b.Add(fp)
        fp.SetPosition(pcbnew.VECTOR2I_MM(X0 + x, Y0 + y))
        fp.SetOrientationDegrees(rot)
        for p in fp.Pads():
            if p.GetNumber() in nets:
                p.SetNet(net(nets[p.GetNumber()]))

    place("U2", MSOP10, 20, 30, 0, U2_NETS)
    place("C10", C0603, 40, 60, 0, {"1": "3V3_UNO", "2": "GND"})
    for i in range(1, 9):  # the optimiser's leftover conflicts, piled on U2
        place(f"R{i}", C0603, 19 + (i % 3), 29 + (i % 4) * 0.7, 0, {"1": f"N{i}", "2": "GND"})
    place("J1", {"pads": [], "court": (-5, -5, 5, 5)}, 30, 30, 0, {})       # fixed
    place("H1", {"pads": [], "court": (-1.7, -1.7, 1.7, 1.7)}, 5, H - 5, 0, {})  # hole at (5, 5)
    place("R9", C0603, 6.5, H - 6.5, 0, {"1": "N9", "2": "GND"})  # under the screw head
    place("R10", C0603, 8, 5, 0, {"1": "N10", "2": "GND"})       # in the antenna keepout
    pcbnew.SaveBoard(str(path), b)


def run(script: str, board: Path, netlist: Path, cfg: Path, *extra: str):
    p = subprocess.run([sys.executable, str(HERE / script), "--board", str(board), "--netlist",
                        str(netlist), "--config", str(cfg), *extra], capture_output=True, text=True)
    js = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    return p.returncode, (js[-1] if js else {}), p.stderr


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def board_tests(tmp: Path) -> None:
    try:
        import pcbnew
    except ImportError:
        print("SKIP  board tests -- no pcbnew (KiCad 9/10) on this interpreter")
        return
    board, netlist, cfg = tmp / "b.kicad_pcb", tmp / "n.csv", tmp / "board.toml"
    netlist.write_text(NETLIST + "N9,R9,1,~,,\nGND,R9,2,~,,\nN10,R10,1,~,,\nGND,R10,2,~,,\n")
    cfg.write_text(CFG)
    make_board(board)

    def poses():
        b = pcbnew.LoadBoard(str(board))
        return {fp.GetReference(): (pcbnew.ToMM(fp.GetPosition()), fp.GetOrientationDegrees())
                for fp in b.GetFootprints()}

    rc, js, err = run("direct_connect.py", board, netlist, cfg, "--stage", "post")
    check("direct_connect: lands C10 on free parts without failing",
          rc == 0 and js.get("placed", [{}])[0].get("target") == "U2.6", err[-400:])
    before = poses()
    rc, js, err = run("pack_blocks.py", board, netlist, cfg)
    check("pack: succeeds and verifies its own write", rc == 0, err[-600:])
    check("pack: U2 and C10 are one block", ["C10", "U2"] in js.get("multi_part_blocks", []),
          str(js.get("multi_part_blocks")))
    after = poses()
    check("pack: J1 and H1 (fixed) did not move", all(after[r] == before[r] for r in ("J1", "H1")))
    (bu, bur), (bc, bcr) = before["U2"], before["C10"]
    (au, aur), (ac, acr) = after["U2"], after["C10"]
    d0 = math.hypot(bc[0] - bu[0], bc[1] - bu[1])
    d1 = math.hypot(ac[0] - au[0], ac[1] - au[1])
    check("pack: C10 moved rigidly with U2", abs(d0 - d1) < 1e-3 and (acr - aur - bcr + bur) % 360 < 1e-6,
          f"{d0:.4f} vs {d1:.4f}")
    check("pack: moves stay local (no block moved more than 12 mm)",
          js.get("max_move_mm", 99) <= 12, str(js.get("max_move_mm")))

    b = pcbnew.LoadBoard(str(board))
    fr = _court = {}
    for fp in b.GetFootprints():
        bb = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
        fr[fp.GetReference()] = tuple(pcbnew.ToMM(v) for v in (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()))
    hole = (X0 + 5 - 3, Y0 + H - 5 - 3, X0 + 5 + 3, Y0 + H - 5 + 3)
    antenna = (X0 + 0, Y0 + H - 68.58, X0 + 20, Y0 + H - 55)
    check("pack: R9 is out from under the screw head", not pb.hits(fr["R9"], hole), str(fr["R9"]))
    check("pack: R10 is out of the keepout", not pb.hits(fr["R10"], antenna), str(fr["R10"]))

    if shutil.which("kicad-cli"):
        out = tmp / "drc.json"
        subprocess.run(["kicad-cli", "pcb", "drc", "-o", str(out), "--format", "json", "--severity-all",
                        str(board)], capture_output=True)
        rep = json.loads(out.read_text())
        overl = sorted(tuple(sorted(i["description"].split()[-1] for i in v["items"]))
                       for v in rep["violations"] if v["type"] == "courtyards_overlap")
        check("DRC: the only courtyard overlap left is the Direct pair",
              overl == [("C10", "U2")], str(overl))
        unconn = [v for v in rep["unconnected_items"] if "3V3_UNO" in json.dumps(v)]
        check("DRC: C10.1-U2.6 still connected after the move", unconn == [], str(unconn))
    else:
        print("SKIP  DRC checks -- no kicad-cli")

    h1 = md5(board)
    rc, js, err = run("pack_blocks.py", board, netlist, cfg)
    check("pack: a second run moves nothing and writes nothing",
          rc == 0 and js.get("moved") == [] and md5(board) == h1, err[-300:])

    # A board with no room at all must fail, not overlap.
    cfg.write_text(CFG.replace('anchors = ["J1"]', 'anchors = ["J1", "BIG"]'))
    b = pcbnew.LoadBoard(str(board))
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference("BIG")
    c = pcbnew.PCB_SHAPE(fp)
    c.SetShape(pcbnew.SHAPE_T_RECT)
    c.SetLayer(pcbnew.F_CrtYd)
    c.SetStart(pcbnew.VECTOR2I_MM(0, 0))
    c.SetEnd(pcbnew.VECTOR2I_MM(W, H))
    fp.Add(c)
    b.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I_MM(X0, Y0))
    pcbnew.SaveBoard(str(board), b)
    h1 = md5(board)
    rc, _, err = run("pack_blocks.py", board, netlist, cfg)
    check("pack: a full board fails naming the block, board untouched",
          rc == 1 and "no legal spot" in err and md5(board) == h1, err[-300:])


def main() -> int:
    search_tests()
    with tempfile.TemporaryDirectory() as t:
        board_tests(Path(t))
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
