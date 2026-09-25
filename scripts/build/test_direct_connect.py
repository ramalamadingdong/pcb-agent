#!/usr/bin/env python3
"""Tests for direct_connect.py.

    python3 scripts/build/test_direct_connect.py

Two halves. The netlist parsing and rectangle maths run anywhere. The
board half needs pcbnew: it builds a small board with pcbnew itself (an
INA226 MSOP-10 and an 0603 cap, pad geometry copied from the example's
vendored footprints), runs the real pass, and judges the result by KiCad's
own DRC -- not by what the pass says about itself. Without pcbnew that half
reports SKIP, and a SKIP is not a PASS.

Board cases:
  held    target anchored: pre places, post and a second post change no byte;
          DRC shows the tagged pins connected and C10 in exactly one
          courtyards_overlap
  free    target not anchored: pre defers, post places, re-run is stable
  blocked every side occupied: exit 1, board untouched
  flipped target sent to the back by the real flip_sides.py: the part follows
          it onto B.Cu and is still connected
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
sys.path.insert(0, str(HERE))

import direct_connect as d  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail and not ok else ''}")


def fails(fn) -> str | None:
    try:
        fn()
    except SystemExit:
        return "exit"
    return None


# ---------------------------------------------------------------- netlist

NETLIST = """\
# comment
Net,RefDes,Pin,PinName,Direct,Note
VIN_PROT,U2,10,IN+,,x
VIN,U2,9,IN-,,x
VIN,U2,8,VBUS,,x
GND,U2,7,GND,,"ground, with a comma"
3V3_UNO,U2,6,VS,,x
SCL_3V3,U2,5,SCL,,x
SDA_3V3,U2,4,SDA,,x
ALERT_3V3,U2,3,Alert,,x
GND,U2,2,A0,,x
GND,U2,1,A1,,x
3V3_UNO,C10,1,~,U2,"100 nF, pad on U2.6"
GND,C10,2,~,,
"""


def netlist_tests(tmp: Path) -> None:
    p = tmp / "n.csv"
    p.write_text(NETLIST)
    tags = d.load_tags(p, {})
    check("tag found with its only candidate",
          [(t.ref, t.pin, t.candidates) for t in tags] == [("C10", "1", (("U2", "6"),))])
    check("held iff a candidate is anchored",
          d.held_refs(p, {}, ["U2"]) == ["C10"] and d.held_refs(p, {}, ["R1"]) == [])
    ex = HERE.parent.parent / "examples" / "unoq-power-shield" / "netlist.csv"
    check("netlist without the column has no tags", d.load_tags(ex, {}) == [])
    check("pin_alias maps netlist pin to pad",
          d.pad_number({"pin_alias": [{"ref": "D1", "pin": "K", "lib_pin": "1"}]}, "D1", "K") == "1")

    bad = {
        "NC row": "NC,C1,1,~,yes,\n",
        "two tags on one part": "A,C1,1,~,yes,\nA,C1,2,~,yes,\nA,U1,1,x,,\n",
        "chain": "A,C1,1,~,C2,\nA,C2,1,~,yes,\nA,U1,1,x,,\n",
        "nothing to touch": "A,C1,1,~,yes,\n",
        "named target not on net": "A,C1,1,~,U9,\nA,U1,1,x,,\n",
    }
    for name, body in bad.items():
        p.write_text("Net,RefDes,Pin,PinName,Direct,Note\n" + body)
        check(f"rejects {name}", fails(lambda: d.load_tags(p, {})) == "exit")
    p.write_text("Net,RefDes,Pin,PinName,Direct,Note\nA,C1,1,~,yes,\nA,U1,1,x,,\n")
    check("rejects a tagged part that is also floorplanned",
          fails(lambda: d.load_tags(p, {"floorplan": {"place": [{"ref": "C1"}]}})) == "exit")


# ---------------------------------------------------------------- geometry

def geometry_tests() -> None:
    F = frozenset("F")
    # SOIC-ish left pin row, 1.27 pitch; 0603 cap at 0 and 180 degrees.
    T = (8.5, 9.7, 10.0, 10.3)
    ic = d.Obstacle("U1", (8.25, 8.0, 14.0, 14.0), [
        ("+3V3", F, T), ("GND", F, (8.5, 10.97, 10.0, 11.57)), ("SDA", F, (8.5, 8.43, 10.0, 9.03))])

    def probe(rot):
        a, b = (-1.175, -0.475, -0.375, 0.475), (0.375, -0.475, 1.175, 0.475)
        if rot == 180:
            a, b = b, a
        return d.Probe(rot, [("1", "+3V3", F, a), ("2", "GND", F, b)], (-1.5, -0.7, 1.5, 0.7))

    ok = []
    for rot in (0, 180):
        for side in d.SIDES:
            res, _ = d.evaluate(probe(rot), "1", "U1", T, F, side, [ic], (0, 0, 50, 50), 0.1, 0.2, (0, 0))
            if res:
                ok.append((rot, side, res[1]))
    check("only the outward pose survives, 0.1 mm into the toe",
          ok == [(180, "left", (7.425, 10.0))], str(ok))
    _, why = d.evaluate(probe(0), "1", "U1", T, F, "right", [ic], (0, 0, 50, 50), 0.1, 0.2, (0, 0))
    check("a pose under the target's body is rejected", "body" in why, why)
    res, _ = d.evaluate(probe(180), "1", "U1", T, F, "left",
                        [ic, d.Obstacle("R9", (6, 9, 7, 11), [])], (0, 0, 50, 50), 0.1, 0.2, (0, 0))
    check("a courtyard collision is counted", res and res[2] == ["R9"])
    res, why = d.evaluate(probe(180), "1", "U1", T, F, "left", [ic], (8, 0, 50, 50), 0.1, 0.2, (0, 0))
    check("off board is rejected", res is None and "off board" in why)
    res, why = d.evaluate(probe(180), "1", "U1", T, frozenset("B"), "left", [ic], (0, 0, 50, 50),
                          0.1, 0.2, (0, 0))
    check("no shared copper layer is rejected", res is None and "layer" in why)


# ---------------------------------------------------------------- board

# From examples/unoq-power-shield/unoq_power_shield.pretty (footprint-local).
MSOP10 = {"pads": [(str(n), (-2.1 if n <= 5 else 2.1), (-1 + 0.5 * (n - 1)) if n <= 5 else (1 - 0.5 * (n - 6)),
                    1.5, 0.35) for n in range(1, 11)],
          "court": (-3.1, -1.77, 3.1, 1.75)}
C0603 = {"pads": [("1", -0.775, 0, 0.9, 0.95), ("2", 0.775, 0, 0.9, 0.95)],
         "court": (-1.48, -0.73, 1.48, 0.73)}
U2_NETS = {"1": "GND", "2": "GND", "3": "ALERT_3V3", "4": "SDA_3V3", "5": "SCL_3V3",
           "6": "3V3_UNO", "7": "GND", "8": "VIN", "9": "VIN", "10": "VIN_PROT"}
X0, Y0, W, H = 100.0, 50.0, 53.34, 68.58


def make_board(path: Path, u2_rot: float, blockers=()) -> None:
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

    def court(fp, r):
        c = pcbnew.PCB_SHAPE(fp)
        c.SetShape(pcbnew.SHAPE_T_RECT)
        c.SetLayer(pcbnew.F_CrtYd)
        c.SetWidth(pcbnew.FromMM(0.05))
        c.SetStart(pcbnew.VECTOR2I_MM(r[0], r[1]))
        c.SetEnd(pcbnew.VECTOR2I_MM(r[2], r[3]))
        fp.Add(c)

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
        court(fp, spec["court"])
        b.Add(fp)
        fp.SetPosition(pcbnew.VECTOR2I_MM(X0 + x, Y0 + y))
        fp.SetOrientationDegrees(rot)
        for p in fp.Pads():
            if p.GetNumber() in nets:
                p.SetNet(net(nets[p.GetNumber()]))

    place("U2", MSOP10, 20, 30, u2_rot, U2_NETS)
    place("C10", C0603, 45, 60, 0, {"1": "3V3_UNO", "2": "GND"})
    for i, (x, y) in enumerate(blockers):
        place(f"X{i}", {"pads": [], "court": (-2.5, -2.5, 2.5, 2.5)}, x, y, 0, {})
    pcbnew.SaveBoard(str(path), b)


def run(script: str, board: Path, netlist: Path, cfg: Path, *extra: str):
    p = subprocess.run([sys.executable, str(HERE / script), "--board", str(board), "--netlist",
                        str(netlist), "--config", str(cfg), *extra], capture_output=True, text=True)
    js = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    return p.returncode, (js[-1] if js else {}), p.stderr


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def drc(board: Path, tmp: Path):
    """(violation (type, refs) pairs touching C10, 3V3_UNO unconnected items) per kicad-cli."""
    out = tmp / "drc.json"
    subprocess.run(["kicad-cli", "pcb", "drc", "-o", str(out), "--format", "json", "--severity-all",
                    str(board)], capture_output=True)
    rep = json.loads(out.read_text())
    mine = [v["type"] for v in rep["violations"] if any("C10" in i["description"] for i in v["items"])]
    unconn = [v for v in rep["unconnected_items"] if "3V3_UNO" in json.dumps(v)]
    return mine, unconn


def board_tests(tmp: Path) -> None:
    try:
        import pcbnew
    except ImportError:
        print("SKIP  board tests -- no pcbnew (KiCad 9/10) on this interpreter")
        return
    have_cli = shutil.which("kicad-cli") is not None
    netlist = tmp / "n.csv"
    netlist.write_text(NETLIST)
    board = tmp / "b.kicad_pcb"
    held = tmp / "held.toml"
    held.write_text('[[floorplan.place]]\nref = "U2"\nx = 20.0\ny = 38.58\n')
    free = tmp / "free.toml"
    free.write_text('[floorplan]\nanchors = ["X9"]\n')

    # held
    make_board(board, 0)
    rc, js, err = run("direct_connect.py", board, netlist, held, "--stage", "pre")
    check("held: pre places C10 against U2.6",
          rc == 0 and [(p["ref"], p["target"], p["held"]) for p in js.get("placed", [])]
          == [("C10", "U2.6", True)], err[-300:])
    h1 = md5(board)
    run("direct_connect.py", board, netlist, held, "--stage", "post")
    run("direct_connect.py", board, netlist, held, "--stage", "post")
    check("held: post, post change no byte", md5(board) == h1)
    if have_cli:
        mine, unconn = drc(board, tmp)
        check("held: KiCad DRC sees C10.1-U2.6 connected", unconn == [], str(unconn))
        check("held: C10 DRC is one courtyards_overlap, no clearance or short",
              mine.count("courtyards_overlap") == 1 and not {"clearance", "shorting_items"} & set(mine),
              str(mine))
    else:
        print("SKIP  held: DRC -- no kicad-cli")

    # free
    make_board(board, 0)
    rc, js, _ = run("direct_connect.py", board, netlist, free, "--stage", "pre")
    check("free: pre defers", rc == 0 and js.get("deferred_to_post") == ["C10"] and not js.get("placed"))
    rc, js, err = run("direct_connect.py", board, netlist, free, "--stage", "post")
    h1 = md5(board)
    run("direct_connect.py", board, netlist, free, "--stage", "post")
    check("free: post places, re-run is stable",
          rc == 0 and len(js.get("placed", [])) == 1 and md5(board) == h1, err[-300:])

    # blocked
    make_board(board, 0, blockers=[(20, 26), (20, 34), (16, 30), (24, 30)])
    h1 = md5(board)
    rc, _, err = run("direct_connect.py", board, netlist, free, "--stage", "post")
    check("blocked: exit 1 naming the blockers, board untouched",
          rc == 1 and "still overlaps" in err and md5(board) == h1, err[-300:])

    # flipped
    make_board(board, 90)
    bot = tmp / "bot.toml"
    bot.write_text('[[floorplan.place]]\nref = "U2"\nx = 20.0\ny = 38.58\nrotation = 90\nside = "bottom"\n')
    run("direct_connect.py", board, netlist, bot, "--stage", "pre")
    run("flip_sides.py", board, netlist, bot)
    rc, js, err = run("direct_connect.py", board, netlist, bot, "--stage", "post")
    b = pcbnew.LoadBoard(str(board))
    layers = {r: b.GetLayerName(b.FindFootprintByReference(r).GetLayer()) for r in ("U2", "C10")}
    check("flipped: C10 follows U2 onto B.Cu",
          rc == 0 and layers == {"U2": "B.Cu", "C10": "B.Cu"}, f"{layers} {err[-300:]}")
    if have_cli:
        _, unconn = drc(board, tmp)
        check("flipped: KiCad DRC sees C10.1-U2.6 connected", unconn == [], str(unconn))


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        netlist_tests(tmp)
        geometry_tests()
        board_tests(tmp)
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
