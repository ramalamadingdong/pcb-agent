#!/usr/bin/env python3
"""check_parity — prove the schematic a human reviews IS the board that ships.

Run on the FINAL board: after routing and every post-route pass
(post_route_fix, finish_routes, cleanup), because those passes change copper
after the schematic was drawn. Each new track and via is given a net by the
pass that made it, and that choice is only a claim. This gate reads what is
actually on the board and fails if the board and the schematic disagree, so
that whatever a pass did either shows up in the schematic or stops the
export.

Four independent looks, because no single tool is trusted about itself:

  1. KiCad's own schematic parity (`kicad-cli pcb drc --schematic-parity`):
     what the reviewer's KiCad will say when they open the project. Must be
     zero items. Missing symbol<->footprint links, a footprint or value that
     changed, a DNP flag that differs, a pad whose net disagrees -- all here.
  2. A diff that shares no code with (1): eeschema's netlist export
     (`kicad-cli sch export netlist`) against every pad pcbnew reads off the
     board. Per part: present on both sides, same value, same footprint id,
     same DNP, same sheet. Per pad: the net the schematic says. A board
     footprint with no schematic symbol must be one board.toml declares as
     mechanical, marked "Not in schematic", with no pad on any net -- so that
     attribute can never hide a circuit change.
  3. Copper really realises those pad nets: DRC sampled --drc-runs times
     (it is not deterministic), failing on any shorting_items /
     tracks_crossing in ANY run, or any unconnected item. Pad nets are only
     labels; this is the geometric check that the copper a post-route pass
     laid between them connects what the labels say and nothing else.
  4. (in validate_gerbers.py, `--kicad-netlist`) the gerbers' own X2 pad
     attributes against the same netlist export -- the bytes the fab plots.

The schematic is never updated from the board. If a post-route pass needs to
change the circuit -- a new part, a pin moved to another net -- that change
goes in netlist.csv and both documents are rebuilt. This gate is what makes
that rule enforceable: anything else fails here.

Writes, beside the board:
  <stem>-netlist.kicad_net   eeschema's netlist export (input to check 4)
  <stem>-parity.json         everything below, for the release manifest

Exit 1 on any disagreement, with every one listed. Needs pcbnew + kicad-cli
of the board's KiCad major (the container has both).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

NAME = "check_parity"

# DRC kinds that mean copper connects something the pad nets say it doesn't.
SHORT_KINDS = {"shorting_items", "tracks_crossing"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def kicad_cli() -> list[str]:
    return shlex.split(os.environ.get("KICAD_CLI", "kicad-cli"))


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    log("+ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        _lib.fail(f"{cmd[0]}: not found (set KICAD_CLI)")


# ---- 1 + 3: KiCad's DRC with schematic parity ------------------------------

def drc_sample(board: Path, out: Path) -> dict:
    p = run(kicad_cli() + ["pcb", "drc", "--schematic-parity", "--severity-all",
                           "--format", "json", "-o", str(out), str(board)])
    if not out.exists():
        _lib.fail(f"kicad-cli pcb drc wrote no report ({p.returncode}): "
                  f"{(p.stderr or p.stdout)[:400]}")
    rep = json.loads(out.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)
    return rep


def describe(item: dict) -> str:
    where = "; ".join(i.get("description", "?") for i in item.get("items", []))
    return f"{item.get('type', '?')}: {item.get('description', '')} [{where}]"


# ---- 2: eeschema netlist vs pcbnew pads -------------------------------------

def read_netlist(path: Path) -> tuple[dict[str, dict], dict[tuple[str, str], str]]:
    """({ref: {value, footprint, dnp, sheetname, sheetfile}}, {(ref, pin): net})."""
    root = _lib.parse_sexpr(path.read_text(encoding="utf-8"))
    comps: dict[str, dict] = {}
    for sec in _lib.sx_children(root, "components"):
        for c in _lib.sx_children(sec, "comp"):
            props = {}
            for p in _lib.sx_children(c, "property"):
                props[_lib.sx_value(p, "name")] = _lib.sx_value(p, "value", "")
            comps[_lib.sx_value(c, "ref")] = {
                "value": _lib.sx_value(c, "value", ""),
                "footprint": _lib.sx_value(c, "footprint", ""),
                # eeschema writes a bare (property (name "dnp")) on DNP parts.
                "dnp": "dnp" in props,
                "sheetname": props.get("Sheetname", ""),
                "sheetfile": props.get("Sheetfile", ""),
            }
    pins: dict[tuple[str, str], str] = {}
    for sec in _lib.sx_children(root, "nets"):
        for n in _lib.sx_children(sec, "net"):
            name = _lib.sx_value(n, "name", "")
            for node in _lib.sx_children(n, "node"):
                pins[(_lib.sx_value(node, "ref"), _lib.sx_value(node, "pin"))] = name
    return comps, pins


def is_auto(net: str) -> bool:
    """Names KiCad invents for a net nobody named. Compared by membership,
    not by name: the two editors spell them differently."""
    return not net or net.startswith(("unconnected-", "Net-("))


def canonical(pins: dict[tuple[str, str], str]) -> dict[tuple[str, str], str | None]:
    """Named nets keep their name; an auto-named net becomes the sorted list
    of its members (so both editors agree), and one with a single member is
    no connection at all (None)."""
    members: dict[str, list] = defaultdict(list)
    for node, net in pins.items():
        members[net].append(node)
    out: dict[tuple[str, str], str | None] = {}
    for node, net in pins.items():
        if not is_auto(net):
            out[node] = net
        elif len(members[net]) < 2:
            out[node] = None
        else:
            out[node] = "auto:" + ",".join(f"{r}.{p}" for r, p in sorted(members[net]))
    return out


def board_view(board: Path) -> tuple[dict[str, dict], dict[tuple[str, str], set[str]]]:
    import pcbnew

    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew returned None loading {board} — KiCad major mismatch?")
    parts: dict[str, dict] = {}
    pads: dict[tuple[str, str], set[str]] = defaultdict(set)
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        if ref in parts:
            _lib.fail(f"{board}: two footprints carry reference {ref}")
        netted = []
        for pad in fp.Pads():
            net = pad.GetNetname()
            if net:
                netted.append(f"{pad.GetNumber() or '(unnumbered)'}={net}")
            if pad.GetNumber():
                pads[(ref, pad.GetNumber())].add(net)
        parts[ref] = {
            "value": fp.GetValue(),
            "footprint": fp.GetFPIDAsString(),
            "dnp": bool(fp.IsDNP()),
            "board_only": bool(fp.GetAttributes() & pcbnew.FP_BOARD_ONLY),
            "path": fp.GetPath().AsString(),
            "sheetname": fp.GetSheetname(),
            "sheetfile": fp.GetSheetfile(),
            "netted_pads": netted,
        }
    return parts, pads


def diff_parts(comps, sch_uuids, parts, pads, sch_pins, mech) -> list[str]:
    bad: list[str] = []
    for ref in sorted(set(comps) | set(parts)):
        c, f = comps.get(ref), parts.get(ref)
        if c and not f:
            bad.append(f"{ref}: in the schematic, not on the board")
            continue
        if f and not c:
            if not f["board_only"]:
                bad.append(f"{ref}: on the board with no schematic symbol and not "
                           "marked 'Not in schematic'")
            if ref not in mech:
                bad.append(f"{ref}: board-only footprint board.toml does not declare "
                           "([mounting_holes] / [fiducials]) — a pass added a part")
            if f["netted_pads"]:
                bad.append(f"{ref}: board-only footprint has pads on nets "
                           f"{f['netted_pads']} — that is a circuit change the "
                           "schematic does not show")
            continue
        if f["board_only"]:
            bad.append(f"{ref}: has a schematic symbol but is marked 'Not in schematic'")
        for key in ("value", "footprint", "dnp", "sheetname", "sheetfile"):
            if c[key] != f[key]:
                bad.append(f"{ref}: {key} differs — schematic {c[key]!r}, board {f[key]!r}")
        want_path = f"/{sch_uuids.get(ref, '?')}"
        if f["path"] != want_path:
            bad.append(f"{ref}: not linked to its symbol (path {f['path']!r}, "
                       f"want {want_path!r}) — link_schematic did not run or was undone")

    # Pads. Board side first: every numbered pad of a schematic part.
    sch = canonical(sch_pins)
    brd_single: dict[tuple[str, str], str] = {}
    for (ref, num), nets in pads.items():
        if ref not in comps:
            continue
        if len(nets) > 1:
            bad.append(f"{ref} pad {num}: one pad number, several nets {sorted(nets)}")
            continue
        brd_single[(ref, num)] = next(iter(nets))
    brd = canonical({k: v for k, v in brd_single.items()})
    for node in sorted(set(sch) | set(brd)):
        s_net = sch.get(node)
        b_net = brd.get(node)
        if node not in brd and node[0] in parts:
            bad.append(f"{node[0]} pin {node[1]}: schematic pin on net {s_net!r} has no "
                       "pad on the footprint")
        elif s_net != b_net:
            bad.append(f"{node[0]} pad {node[1]}: schematic says {s_net or 'no net'}, "
                       f"board says {b_net or 'no net'}")
    return bad


def main() -> int:
    ap = _lib.pass_parser(NAME, board=True, schematic=False, netlist=True)
    ap.add_argument("--schematic", default=None,
                    help="default: the .kicad_sch beside --board with the same stem")
    ap.add_argument("--drc-runs", type=int, default=5,
                    help="DRC samples; DRC is not deterministic (min 1)")
    args = ap.parse_args()

    board = Path(args.board)
    sch = Path(args.schematic) if args.schematic else board.with_suffix(".kicad_sch")
    for p in (board, sch):
        if not p.exists():
            _lib.fail(f"{p}: not found")
    cfg = _lib.load_config(args.config)
    mech = _lib.mechanical_parts(cfg)
    net_out = board.with_name(f"{board.stem}-netlist.kicad_net")
    report_out = board.with_name(f"{board.stem}-parity.json")

    # ---- 1 + 3 ---------------------------------------------------------------
    runs = []
    for i in range(max(1, args.drc_runs)):
        rep = drc_sample(board, board.with_name(f".parity.{os.getpid()}.{i}.json"))
        runs.append(rep)
        kinds = Counter(v.get("type") for v in rep.get("violations", []))
        log(f"    drc {i + 1}: parity {len(rep.get('schematic_parity', []))}, "
            f"unconnected {len(rep.get('unconnected_items', []))}, "
            f"shorts {sum(kinds[k] for k in SHORT_KINDS)}")
    parity_items = sorted({describe(x) for r in runs for x in r.get("schematic_parity", [])})
    short_items = sorted({describe(v) for r in runs for v in r.get("violations", [])
                          if v.get("type") in SHORT_KINDS})
    unconnected = sorted({describe(x) for r in runs for x in r.get("unconnected_items", [])})

    # ---- 2 -------------------------------------------------------------------
    p = run(kicad_cli() + ["sch", "export", "netlist", "--format", "kicadsexpr",
                           "-o", str(net_out), str(sch)])
    if p.returncode != 0 or not net_out.exists():
        _lib.fail(f"kicad-cli sch export netlist failed ({p.returncode}): "
                  f"{(p.stderr or p.stdout)[:400]}")
    # The export stamps the wall-clock time and the absolute path it was run
    # from. Neither is design content; left in, the same design never
    # produces the same file, and a release could not tell "unchanged" from
    # "changed". Blank both, keep the file name.
    text = net_out.read_text(encoding="utf-8")
    text = re.sub(r'\(date "[^"]*"\)', '(date "")', text, count=1)
    text = re.sub(r'\(source "[^"]*"\)', f'(source "{sch.name}")', text, count=1)
    net_out.write_text(text, encoding="utf-8")
    comps, sch_pins = read_netlist(net_out)
    _, symbols = _lib.schematic_symbols(sch)
    sch_uuids = {r: s["uuid"] for r, s in symbols.items()}
    comps = {r: c for r, c in comps.items() if r in symbols and symbols[r]["on_board"]}
    parts, pads = board_view(board)
    part_diffs = diff_parts(comps, sch_uuids, parts, pads, sch_pins, mech)

    ok = not (parity_items or short_items or unconnected or part_diffs)
    report = {
        "board": board.name,
        "schematic": sch.name,
        "ok": ok,
        "drc_runs": len(runs),
        "kicad_version": runs[0].get("kicad_version"),
        "kicad_parity": parity_items,
        "shorts": short_items,
        "unconnected": unconnected,
        "independent_diff": part_diffs,
        "parts_linked": len(comps),
        "board_only": sorted(r for r, f in parts.items() if f["board_only"]),
        "pads_compared": len(pads),
    }
    report_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for title, items in (("KiCad schematic parity", parity_items),
                         ("copper shorts (any DRC run)", short_items),
                         ("unconnected items (any DRC run)", unconnected),
                         ("schematic netlist vs board pads", part_diffs)):
        log(f"  {'PASS' if not items else 'FAIL'}  {title}: {len(items)}")
        for it in items[:40]:
            log(f"        {it}")
        if len(items) > 40:
            log(f"        ... {len(items) - 40} more in {report_out.name}")

    _lib.emit(NAME, board=str(board), ok=ok, kicad_parity=len(parity_items),
              shorts=len(short_items), unconnected=len(unconnected),
              independent_diff=len(part_diffs), parts=len(comps),
              board_only=report["board_only"], netlist=str(net_out),
              report=str(report_out))
    if not ok:
        _lib.fail("PARITY FAILED: the board is not the schematic. Fix it in "
                  "netlist.csv or the pass that caused it, rebuild — never by "
                  "editing either file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
