#!/usr/bin/env python3
"""Tests for the schematic layout: sch_layout.py and generate_schematic.py.

    python3 scripts/build/test_schematic_layout.py

The packer half runs anywhere. The schematic half needs kicad_tools and
KiCad's stock symbols (Device.kicad_sym under /usr/share/kicad/symbols, or
$KICAD_SYMBOL_DIR): it regenerates a copy of the example with six caps tagged
Direct, and judges the result with kicad-cli's own netlist export where it
can, not by what the generator says about itself. Without those tools that
half reports SKIP, and a SKIP is not a PASS.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1] / "examples" / "unoq-power-shield"
sys.path.insert(0, str(HERE))

import sch_layout as sl  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail and not ok else ''}")


def on_grid(v: float) -> bool:
    return abs(v / sl.GRID - round(v / sl.GRID)) < 1e-6


# ---------------------------------------------------------------- packer

def packer_tests() -> None:
    items = [(f"b{i}", w, h) for i, (w, h) in enumerate(
        [(40, 30), (12, 8), (12, 8), (60, 12), (8, 40), (25, 25), (5, 5), (70, 9), (15, 15)])]
    out = sl.skyline_pack(items, 120)
    boxes = {k: (x, y, x + sl.snap_up(w + 2 * sl.GRID), y + sl.snap_up(h + 2 * sl.GRID))
             for (k, w, h), (x, y) in zip(items, [out[k] for k, _, _ in items])}
    check("every block placed", set(out) == {k for k, _, _ in items})
    check("no two blocks overlap",
          not any(sl.hits(boxes[a], boxes[b]) for a in boxes for b in boxes if a < b))
    check("all inside the width", all(b[2] <= 120 + 1e-6 for b in boxes.values()))
    check("offsets on the 2.54 grid", all(on_grid(x) and on_grid(y) for x, y in out.values()))
    check("small blocks fill in beside tall ones (not one per row)",
          max(b[3] for b in boxes.values()) < sum(h for _, _, h in items))
    wide = sl.skyline_pack([("w", 500, 10), ("n", 10, 10)], 120)
    check("a block wider than the sheet still goes in", set(wide) == {"w", "n"})
    check("label boxes point where asked",
          sl.label_box(0, 0, (1, 0), "GND")[0] == 0 and sl.label_box(0, 0, (-1, 0), "GND")[2] == 0
          and sl.label_box(0, 0, (0, 1), "GND")[1] == 0 and sl.label_box(0, 0, (0, -1), "GND")[3] == 0)


# ---------------------------------------------------------------- schematic

TAGS = {("C10", "1"): "U2", ("C1", "1"): "yes", ("C2", "1"): "yes", ("C3", "1"): "yes",
        ("C4", "1"): "yes", ("C5", "1"): "yes"}
# What each tag must hang on: the preferred target. For "yes" on VIN that is
# U1.2 (TPS54331 VIN, power_in), not U2.9 (INA226 IN-, input) -- even though
# U2 has more netlist rows.
WANT = {"C10": ("U2", "6"), "C1": ("U1", "2"), "C2": ("U1", "2"), "C3": ("U1", "2"),
        "C4": ("U1", "1"), "C5": ("U1", "4")}


def tagged_copy(tmp: Path) -> tuple[Path, Path]:
    for f in EXAMPLE.iterdir():
        if f.is_file() or f.suffix == ".pretty":
            (shutil.copytree if f.is_dir() else shutil.copy2)(f, tmp / f.name)
    toml = (tmp / "board.toml").read_text()
    for ref in {r for r, _ in TAGS}:
        toml, n = re.subn(r'\[\[floorplan\.place\]\]\nref = "%s"\n(?:(?!\[\[).*\n)*' % ref, "", toml, count=1)
        assert n == 1, ref
    (tmp / "d.toml").write_text(toml)
    rows = list(csv.reader((tmp / "netlist.csv").open()))
    with (tmp / "d.csv").open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        for r in rows:
            if not r or r[0].startswith("#"):
                w.writerow(r)
            elif r[0] == "Net":
                w.writerow(r[:4] + ["Direct"] + r[4:])
            else:
                r = r + [""] * (5 - len(r))
                w.writerow(r[:4] + [TAGS.get((r[1], r[2]), "")] + r[4:])
    return tmp / "d.csv", tmp / "d.toml"


def generate(sch: Path, netlist: Path, cfg: Path):
    p = subprocess.run([sys.executable, str(HERE / "generate_schematic.py"), "--schematic", str(sch),
                        "--netlist", str(netlist), "--config", str(cfg)],
                       capture_output=True, text=True, cwd=cfg.parent)
    js = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    return p.returncode, (js[-1] if js else {}), p.stderr


def schematic_tests(tmp: Path) -> None:
    try:
        import kicad_tools  # noqa: F401
    except ImportError:
        print("SKIP  schematic tests -- no kicad_tools")
        return
    sym_dir = Path(os.environ.get("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols"))
    if not (sym_dir / "Device.kicad_sym").exists():
        print("SKIP  schematic tests -- no stock KiCad symbols (set KICAD_SYMBOL_DIR)")
        return
    import generate_schematic as gs
    from kicad_tools.schematic.models.schematic import Schematic

    netlist, cfg = tagged_copy(tmp)
    plain = tmp / "plain.kicad_sch"
    rc, js, err = generate(plain, tmp / "netlist.csv", tmp / "board.toml")
    check("untagged example: round-trip passes",
          rc == 0 and js.get("round_trip_nodes") == 159, err[-400:])
    out = tmp / "d.kicad_sch"
    rc, js, err = generate(out, netlist, cfg)
    check("tagged: round-trip passes", rc == 0 and js.get("round_trip_nodes") == 159, err[-400:])
    check("tagged: no part fell back to its own block", "WARNING" not in err, err[-400:])
    first = out.read_text()
    generate(out, netlist, cfg)
    again = out.read_text()

    def drawn(t: str) -> str:
        """The sheet minus its embedded (lib_symbols ...) copy of the libraries."""
        i = t.index("(lib_symbols")
        return t[:i] + t[t.index("\n\t)\n", i) + 4:]

    check("tagged: a re-run draws the same sheet (placement, labels, wires)",
          drawn(again) == drawn(first))
    if again != first:
        print("NOTE  the embedded lib_symbols differ between runs: kicad_tools copies some stock "
              "symbols' properties in hash order (PYTHONHASHSEED). Not layout; predates it.")

    s = Schematic.load(out)
    syms = {sy.reference: sy for sy in s.symbols}
    ends = {(round(x, 2), round(y, 2)) for w in s.wires for x, y in ((w.x1, w.y1), (w.x2, w.y2))}
    labels = {(round(l.x, 2), round(l.y, 2)) for l in getattr(s, "global_labels", [])}
    for ref, (tref, tpin) in WANT.items():
        pin = syms[ref].pin_position("1")
        tp = syms[tref].pin_position(tpin)
        check(f"{ref}.1 drawn on a wire from {tref}.{tpin}",
              pin in ends and tp in ends and pin not in labels and tp not in labels,
              f"pin {pin} target {tp}")

    boxes = {r: gs.symbol_box(sy) for r, sy in syms.items()}
    clash = [(a, b) for a in boxes for b in boxes if a < b and sl.hits(boxes[a], boxes[b])]
    check("tagged: no two symbols overlap", not clash, str(clash[:5]))

    if shutil.which("kicad-cli"):
        xml = tmp / "d.xml"
        subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml),
                        str(out)], capture_output=True)
        nets = {n.get("name").lstrip("/"): {(x.get("ref"), x.get("pin")) for x in n.iter("node")}
                for n in ET.parse(xml).getroot().iter("net")}
        ok = all((ref, "1") in nets.get(net, set()) and want in nets.get(net, set())
                 for ref, want, net in [("C10", ("U2", "6"), "3V3_UNO"), ("C1", ("U1", "2"), "VIN"),
                                        ("C4", ("U1", "1"), "BOOT"), ("C5", ("U1", "4"), "SS")])
        check("kicad-cli's own netlist puts each tagged pin on its target's net", ok)
    else:
        print("SKIP  kicad-cli netlist cross-check -- no kicad-cli")


def main() -> int:
    packer_tests()
    with tempfile.TemporaryDirectory() as t:
        schematic_tests(Path(t))
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
