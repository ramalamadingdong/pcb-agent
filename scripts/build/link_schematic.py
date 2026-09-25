#!/usr/bin/env python3
"""link_schematic — tie every board footprint to its schematic symbol.

KiCad knows a footprint belongs to a symbol through three fields on the
footprint: `(path "/<symbol uuid>")`, `(sheetname ...)` and `(sheetfile ...)`.
`kct create-pcb` writes none of them, and it writes the footprint id without
its library nickname. The board still builds and routes -- but when a human
opens the project, KiCad treats the schematic and the board as two unrelated
documents: no cross-probing, no highlight-net across editors, 54 parity errors
on the example board, and "Update PCB from Schematic" (F8) with default
options proposes deleting every placed footprint and adding fresh unplaced
ones. The reviewer's KiCad would say the files disagree even where they don't.

This pass writes those fields from the generated schematic, so the project a
human opens is ONE design as far as KiCad is concerned:

  * each footprint whose reference has a schematic symbol gets that symbol's
    path, the root sheet name/file, and the symbol's Footprint field as its
    footprint id (library nickname included);
  * each footprint board.toml puts on the board that is not a circuit part --
    mounting holes and fiducials, see _lib.mechanical_parts -- is marked
    "Not in schematic" (board_only), KiCad's own declaration for exactly that;
  * anything else fails the pass: a footprint with no symbol that board.toml
    does not declare, or a symbol with no footprint. A pass that added or
    dropped a part would otherwise ship a board the schematic doesn't show.

This pass only LINKS. It never makes the board agree with the schematic --
check_parity is the gate that proves they agree, on the final board, after
every post-route pass has had its say.

Runs at the end of `make build`, after every pass that adds footprints and
after the kct fill (a kct write is not trusted to keep fields it doesn't
model), immediately before the snapshot. pcbnew keeps the fields through
every later load/save, and check_parity fails if one ever goes missing.

Idempotent: every value is derived from the schematic and board.toml; a
second run finds nothing to change and does not rewrite the board.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

NAME = "link_schematic"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def write_lib_tables(cfg: dict, config_path: Path, board_path: Path) -> list[str]:
    """fp-lib-table and sym-lib-table beside the board, naming the project
    library by a ${KIPRJMOD}-relative path.

    Once the footprints carry their library nickname, KiCad looks that
    library up -- and a project with no table makes the reviewer's KiCad
    report every footprint's library missing (lib_footprint_issues in DRC)
    and leaves "Update footprint from library" pointing at nothing. Stock
    libraries (Device, Connector, ...) resolve through the reviewer's global
    tables and are not listed. Written whole each run, so it is idempotent.
    """
    libs = cfg.get("libs") or {}
    name = libs.get("name")
    if not name:
        _lib.fail("board.toml needs [libs] name — the project library nickname")
    cfg_dir = config_path.resolve().parent
    out_dir = Path(libs.get("out_dir", "."))
    out_dir = out_dir if out_dir.is_absolute() else cfg_dir / out_dir
    here = board_path.resolve().parent
    # [project] footprint_lib resolves against the board's directory, the
    # way add_mounting_holes.footprint_lib reads it.
    fp_raw = (cfg.get("project") or {}).get("footprint_lib")
    if fp_raw:
        fp_dir = Path(fp_raw) if Path(fp_raw).is_absolute() else here / fp_raw
    else:
        fp_dir = out_dir / f"{name}.pretty"
    sym_file = out_dir / f"{name}.kicad_sym"

    def uri(p: Path) -> str:
        try:
            return "${KIPRJMOD}/" + p.resolve().relative_to(here).as_posix()
        except ValueError:
            _lib.fail(f"{p}: project library is outside the project directory "
                      f"{here} — a reviewer's copy of the project could not find it")

    written = []
    for table, kind, lib in (("fp-lib-table", "fp_lib_table", fp_dir),
                             ("sym-lib-table", "sym_lib_table", sym_file)):
        if not lib.exists():
            _lib.fail(f"{lib}: project library not found — run make_libs")
        text = (f"({kind}\n\t(version 7)\n"
                f'\t(lib (name "{name}")(type "KiCad")(uri "{uri(lib)}")(options "")'
                f'(descr "project library, vendored by make_libs"))\n)\n')
        path = here / table
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            written.append(table)
    return written


def main() -> int:
    ap = _lib.pass_parser(NAME, board=True, schematic=False, netlist=True)
    ap.add_argument("--schematic", default=None,
                    help="default: the .kicad_sch beside --board with the same stem")
    args = ap.parse_args()

    board_path = Path(args.board)
    sch_path = Path(args.schematic) if args.schematic else board_path.with_suffix(".kicad_sch")
    if not board_path.exists():
        _lib.fail(f"{board_path}: no such board")
    if not sch_path.exists():
        _lib.fail(f"{sch_path}: no schematic to link against — run generate_schematic")

    cfg = _lib.load_config(args.config)
    mech = _lib.mechanical_parts(cfg)
    root_uuid, symbols = _lib.schematic_symbols(sch_path)
    clash = sorted(set(mech) & set(symbols))
    if clash:
        _lib.fail(f"{clash}: declared as mechanical in board.toml AND drawn as schematic "
                  "symbols — one part, two owners")

    import pcbnew

    b = pcbnew.LoadBoard(str(board_path))
    if b is None:
        _lib.fail(f"pcbnew returned None loading {board_path} — KiCad major mismatch?")

    board_only_flag = pcbnew.FP_BOARD_ONLY
    changed: list[str] = []
    linked = 0
    marked: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    sheetfile = sch_path.name
    # What eeschema itself reports for the root sheet (its netlist export's
    # Sheetname property, checked against in check_parity): the file stem.
    sheetname = sch_path.stem

    for fp in b.GetFootprints():
        ref = fp.GetReference()
        if ref in seen:
            _lib.fail(f"{board_path}: two footprints carry reference {ref}")
        seen.add(ref)

        if ref in symbols:
            s = symbols[ref]
            if not s["on_board"]:
                _lib.fail(f"{ref}: symbol is marked 'not on board' but the board has it")
            want_path = f"/{s['uuid']}"
            touched = False
            if fp.GetPath().AsString() != want_path:
                fp.SetPath(pcbnew.KIID_PATH(want_path))
                touched = True
            if fp.GetSheetname() != sheetname:
                fp.SetSheetname(sheetname)
                touched = True
            if fp.GetSheetfile() != sheetfile:
                fp.SetSheetfile(sheetfile)
                touched = True
            if s["footprint"]:
                nick, _, item = s["footprint"].rpartition(":")
                if not item:
                    _lib.fail(f"{ref}: schematic Footprint field {s['footprint']!r} "
                              "is not a valid library id")
                lid = pcbnew.LIB_ID(nick, item)
                if str(lid.GetLibItemName()) != str(fp.GetFPID().GetLibItemName()):
                    # Same library item or it is a different part. Linking a
                    # renamed footprint would hide exactly what parity is for.
                    _lib.fail(
                        f"{ref}: board footprint {fp.GetFPIDAsString()!r} is not the "
                        f"schematic's {s['footprint']!r} — rebuild; never relabel")
                if fp.GetFPIDAsString() != s["footprint"]:
                    fp.SetFPID(lid)
                    touched = True
            # Do-not-populate is the schematic's call, and kct create-pcb
            # drops it. Carried over exactly as KiCad's own Update PCB does;
            # left unset, the board would quietly disagree with the drawing
            # about which parts get fitted.
            if fp.IsDNP() != s["dnp"]:
                fp.SetDNP(s["dnp"])
                touched = True
            if fp.GetAttributes() & board_only_flag:
                fp.SetAttributes(fp.GetAttributes() & ~board_only_flag)
                touched = True
            linked += 1
            if touched:
                changed.append(ref)
        elif ref in mech:
            if not fp.GetAttributes() & board_only_flag:
                fp.SetAttributes(fp.GetAttributes() | board_only_flag)
                changed.append(ref)
            marked.append(ref)
        else:
            unknown.append(ref)

    if unknown:
        _lib.fail(
            f"footprints on the board with no schematic symbol: {sorted(unknown)}. "
            "A circuit part belongs in netlist.csv; a mechanical one in "
            "[mounting_holes] / [fiducials]. Nothing else may add a footprint.")
    missing = sorted(r for r, s in symbols.items() if s["on_board"] and r not in seen)
    if missing:
        _lib.fail(f"schematic symbols with no footprint on the board: {missing}")
    absent_mech = sorted(set(mech) - seen)
    if absent_mech:
        log(f"  note: board.toml declares {absent_mech} but they are not on the board "
            "yet (their pass has not run?)")

    tables = write_lib_tables(cfg, Path(args.config), board_path)

    if changed:
        pcbnew.SaveBoard(str(board_path), b)
        _lib.assert_net_table(board_path)
        log(f"  linked {linked} footprint(s) to {sch_path.name}; "
            f"{len(marked)} marked not-in-schematic; {len(changed)} rewritten")
    else:
        log(f"  already linked ({linked} footprints, {len(marked)} board-only) — board untouched")

    _lib.emit(NAME, board=str(board_path), schematic=str(sch_path), root_uuid=root_uuid,
              linked=linked, board_only=sorted(marked), changed=sorted(changed),
              wrote=bool(changed), lib_tables=tables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
