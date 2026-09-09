#!/usr/bin/env python3
"""export_fab — board -> fab package (gerbers + drill + pos [+ JLC BOM/CPL]).

The export pins the coordinate contract the whole repo depends on: the
board's aux (drill/place) origin is set to the Edge.Cuts BOTTOM-LEFT
corner, and every artifact is exported against it. That makes gerber and
drill coordinates equal to the canonical board frame (origin bottom-left,
X right, Y up) that board.toml's [frame], [[keepouts]], positions — and
scripts/validate_gerbers.py — all speak. Never export without the aux
origin: KiCad then uses page coordinates with Y negated, every rectangle
in board.toml points at the wrong place, and only the [frame] check
stands between that and a keepout that "passes" on empty space.

X2 attributes stay ON (kicad-cli default): net names and the fiducial
AperFunction ride in them, and without them the checker's net-aware and
fiducial checks go blind.

Config (board.toml):
  [fab]
  name        = "my-board"      # file prefix; default: board file stem
  out_dir     = "fab"           # relative to board.toml
  layers      = "F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts"
  zip         = true
  [fab.jlc]                     # optional: JLCPCB assembly BOM/CPL
  bom_csv     = "bom.csv"       # JLC-format source BOM (Comment/Designator/Footprint/LCSC Part #)
  rot180      = ["Q1", "U4"]    # KiCad pin-1 upper-left vs LCSC lower-left family
"""

from __future__ import annotations

import csv
import os
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _lib  # noqa: E402

DEFAULT_LAYERS = (
    "F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,B.Mask,"
    "F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def run(cmd: list[str]) -> None:
    log("  $ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stdout.strip():
        log(p.stdout.strip())
    if p.returncode != 0:
        _lib.fail(f"command failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")


def main() -> int:
    ap = _lib.pass_parser("export_fab", netlist=False)
    ap.add_argument("--out", default=None, help="output dir (default [fab].out_dir or 'fab' beside the config)")
    args = ap.parse_args()

    cfg = _lib.load_config(args.config)
    fab_cfg = cfg.get("fab", {})
    cfg_dir = Path(args.config).resolve().parent
    board_path = Path(args.board)
    name = fab_cfg.get("name") or cfg.get("board", {}).get("name") or board_path.stem
    out_dir = Path(args.out) if args.out else cfg_dir / fab_cfg.get("out_dir", "fab")
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = fab_cfg.get("layers", DEFAULT_LAYERS)
    kcli = shlex.split(os.environ.get("KICAD_CLI", "kicad-cli"))

    # --- 1. pin the aux origin to the outline bottom-left (idempotent) ---
    import pcbnew

    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        _lib.fail(f"pcbnew returned None loading {board_path} — KiCad older than the board format?")
    x_left, y_bottom = _lib.board_frame(board)
    origin = pcbnew.VECTOR2I(pcbnew.FromMM(x_left), pcbnew.FromMM(y_bottom))
    ds = board.GetDesignSettings()
    if ds.GetAuxOrigin() != origin:
        ds.SetAuxOrigin(origin)
        pcbnew.SaveBoard(str(board_path), board)
        log(f"aux origin -> board bottom-left ({x_left:.3f}, {y_bottom:.3f}) kicad-mm")
    _lib.assert_net_table(board_path)

    # --- 2. gerbers (X2 on by default — required), drill, pos ---
    run(kcli + ["pcb", "export", "gerbers", str(board_path), "-o", f"{out_dir}/",
                "--layers", layers, "--no-protel-ext", "--use-drill-file-origin"])
    run(kcli + ["pcb", "export", "drill", str(board_path), "-o", f"{out_dir}/",
                "--format", "excellon", "--drill-origin", "plot",
                "--excellon-separate-th", "--excellon-units", "mm"])
    pos_csv = out_dir / f"{name}-top-pos.csv"
    run(kcli + ["pcb", "export", "pos", str(board_path), "-o", str(pos_csv),
                "--format", "csv", "--units", "mm", "--side", "front",
                "--use-drill-file-origin"])

    # --- 3. optional JLC assembly BOM/CPL ---
    jlc = fab_cfg.get("jlc")
    jlc_files = []
    if jlc and jlc.get("bom_csv"):
        jdir = out_dir / "jlcpcb"
        jdir.mkdir(exist_ok=True)
        rot180 = set(jlc.get("rot180", []))
        bom_src = cfg_dir / jlc["bom_csv"]
        if not bom_src.is_file():
            # The gerbers and drill files above are already on disk and are
            # valid without this; only the assembly package is missing. Say
            # exactly that instead of a FileNotFoundError traceback, and still
            # exit non-zero -- [fab.jlc] declares an assembly order, and an
            # assembly order with no BOM is not a package a fab will build.
            _lib.fail(f"[fab.jlc] bom_csv names {bom_src.name}, which does not exist. "
                 f"Gerbers/drill are exported to {out_dir}; the JLC BOM/CPL are "
                 f"not. Build {bom_src.name} (Comment, Designator, Footprint, "
                 f"LCSC Part #) from sourced part numbers -- the bom skill -- "
                 f"or drop the [fab.jlc] block until parts are chosen.")
        rows = list(csv.DictReader(open(bom_src, encoding="utf-8")))
        bom_refs: set[str] = set()
        bom_out = jdir / f"{name}_BOM.csv"
        with open(bom_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
            for r in rows:
                refs = r["Designator"].replace(",", " ").split()
                bom_refs.update(refs)
                w.writerow([r["Comment"], ",".join(refs), r["Footprint"], r["LCSC Part #"]])
        pos = list(csv.DictReader(open(pos_csv, encoding="utf-8")))
        kept, dropped = [], []
        for r in pos:
            ref = r["Ref"]
            if ref not in bom_refs:
                dropped.append(ref)  # CPL orphans (fiducials, holes) make fabs reject orders
                continue
            rot = float(r["Rot"])
            if ref in rot180:
                rot = (rot + 180.0) % 360.0
            kept.append([ref, r["Val"], r["Package"], r["PosX"], r["PosY"], f"{rot:g}", "top"])
        cpl_out = jdir / f"{name}_CPL.csv"
        with open(cpl_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Designator", "Comment", "Footprint", "Mid X", "Mid Y", "Rotation", "Layer"])
            w.writerows(kept)
        log(f"JLC BOM {len(rows)} lines / {len(bom_refs)} placements; CPL kept {len(kept)}, dropped {sorted(dropped)}")
        missing = bom_refs - {k[0] for k in kept}
        if missing:
            _lib.fail(f"BOM refs missing from CPL: {sorted(missing)}")
        jlc_files = [bom_out.name, cpl_out.name]

    # --- 4. zip ---
    zip_name = None
    if fab_cfg.get("zip", True):
        zip_path = out_dir / f"{name}_gerbers.zip"
        zip_path.unlink(missing_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out_dir.iterdir()):
                if f.suffix.lower() in (".gbr", ".drl", ".gbrjob"):
                    z.write(f, f.name)
            n = len(z.namelist())
        log(f"zipped {n} files -> {zip_path.name}")
        zip_name = zip_path.name

    _lib.emit("export_fab", out_dir=str(out_dir), zip=zip_name, jlc=jlc_files,
              aux_origin=[x_left, y_bottom])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
