"""Shared helpers for build passes.

Every pass in this directory satisfies the same contract (see the
repository README): takes --board (or --schematic) and --netlist, reads
generalized parameters from board.toml via --config, is idempotent, exits
non-zero on failure, and prints exactly one JSON line on success so the
agent can verify what changed rather than assume.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


def pass_parser(
    name: str,
    *,
    board: bool = True,
    schematic: bool = False,
    netlist: bool = True,
) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=name)
    if board:
        ap.add_argument("--board", required=True, help="path to .kicad_pcb")
    if schematic:
        ap.add_argument("--schematic", required=True, help="path to .kicad_sch")
    if netlist:
        ap.add_argument("--netlist", default="netlist.csv")
    ap.add_argument("--config", default="board.toml", help="board.toml with the generalized parameters")
    return ap


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def emit(name: str, **changed) -> None:
    """The one JSON line. Everything else a pass prints goes to stderr."""
    print(json.dumps({"pass": name, **changed}))


def fail(msg: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def assert_net_table(board_path: str | Path) -> int:
    """Fail loudly if a board write stripped the top-level net table.

    One KiCad operation rewrites the board into a stripped format that
    drops it. KiCad opens the file fine, DRC runs fine, every fab export
    works — and the router then reports "nets to route: 0" and does
    nothing, silently. Run this after every pass that writes the board.
    """
    text = Path(board_path).read_text(encoding="utf-8", errors="replace")
    # Two dialects exist: the numbered table `(net 1 "GND")` (kct and
    # KiCad <= 8 style) and the name-only references `(net "GND")` that
    # pcbnew 10's SaveBoard writes. Either proves nets survived the write;
    # zero of both is the stripped-table disaster.
    n = len(re.findall(r'^\s*\(net\s+\d+\s+"', text, re.M))
    n_named = len(re.findall(r'\(net\s+"[^"]+"\)', text))
    if n <= 1 and n_named == 0:  # net 0 "" is always present, even stripped
        fail(
            f"{board_path}: net table is empty ({n} numbered, {n_named} named "
            "net entries) — a board write stripped it; the router would "
            "silently no-op"
        )
    return max(n, n_named)


def board_frame(board) -> tuple[float, float]:
    """(x_left, y_bottom) of the Edge.Cuts bbox, in KiCad mm.

    board.toml speaks ONE coordinate frame everywhere: origin at the
    board's bottom-left corner, X right, Y UP — the frame of a drill
    file or gerber exported with the aux origin at that corner, and the
    frame `[frame].outline_bbox` asserts. KiCad pages run Y DOWN, so a
    pass consuming config coordinates converts:
        kicad_x = x_left + bx ; kicad_y = y_bottom - by
    Never write a config rectangle straight into pcbnew coordinates —
    the checker would then inspect the mirror image of where you drew.
    """
    import pcbnew

    bbox = board.GetBoardEdgesBoundingBox()
    return (
        pcbnew.ToMM(bbox.GetLeft()),
        pcbnew.ToMM(bbox.GetBottom()),
    )


def to_kicad_xy(frame: tuple[float, float], bx: float, by: float) -> tuple[float, float]:
    """Board-frame (bottom-left, Y-up) -> KiCad page mm (Y-down)."""
    x_left, y_bottom = frame
    return (x_left + bx, y_bottom - by)


def count_segments(board_path: str | Path) -> int:
    """Copper segment count, for before/after router verification —
    a correct net list plus zero new copper is a silent no-op."""
    text = Path(board_path).read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"^\s*\(segment\s", text, re.M))


# ---- schematic <-> board identity -------------------------------------------

def parse_sexpr(text: str) -> list:
    """Parse one KiCad s-expression into nested lists of str.

    Quoted atoms come back unquoted (with \\" and \\\\ unescaped), bare atoms
    as-is. Small and dependency-free on purpose: the parity passes read the
    schematic without kicad-tools, so the check does not share a parser with
    the generator it is checking.
    """
    tok = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = tok.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"s-expression parse error at offset {pos}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(re.sub(r"\\(.)", r"\1", m.group(3)))
        elif m.group(4) is not None:
            stack[-1].append(m.group(4))
    if len(stack) != 1 or not stack[0]:
        raise ValueError("unbalanced s-expression")
    return stack[0][0]


def sx_children(node: list, head: str) -> list[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def sx_value(node: list, head: str, default=None):
    for c in sx_children(node, head):
        if len(c) > 1:
            return c[1]
    return default


def schematic_symbols(sch_path: str | Path) -> tuple[str, dict[str, dict]]:
    """(root sheet uuid, {ref: {uuid, lib_id, footprint, value, on_board}}).

    Flat (single-sheet) schematics only -- which is what generate_schematic
    writes. The reference is taken from the symbol's instance entry for the
    root sheet path, which is what eeschema displays, falling back to the
    Reference property. Power symbols and flags (#PWR, #FLG) are skipped:
    they are never on a board.
    """
    root = parse_sexpr(Path(sch_path).read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_sch":
        fail(f"{sch_path}: not a .kicad_sch")
    if sx_children(root, "sheet"):
        fail(f"{sch_path}: hierarchical sheets are not supported by the parity passes")
    root_uuid = sx_value(root, "uuid")
    if not root_uuid:
        fail(f"{sch_path}: schematic has no root uuid")
    out: dict[str, dict] = {}
    for sym in sx_children(root, "symbol"):
        props = {p[1]: p[2] for p in sx_children(sym, "property") if len(p) > 2}
        ref = props.get("Reference", "")
        for inst in sx_children(sym, "instances"):
            for proj in sx_children(inst, "project"):
                for path in sx_children(proj, "path"):
                    if len(path) > 1 and path[1] == f"/{root_uuid}":
                        ref = sx_value(path, "reference", ref)
        if not ref or ref.startswith("#"):
            continue
        if ref in out:
            fail(f"{sch_path}: reference {ref} appears on two symbols")
        out[ref] = {
            "uuid": sx_value(sym, "uuid"),
            "lib_id": sx_value(sym, "lib_id"),
            "footprint": props.get("Footprint", ""),
            "value": props.get("Value", ""),
            "on_board": sx_value(sym, "on_board", "yes") != "no",
            "dnp": sx_value(sym, "dnp", "no") == "yes",
        }
    return root_uuid, out


def mechanical_parts(cfg: dict) -> dict[str, str]:
    """{ref: what} for the footprints board.toml puts on the board that are
    not circuit parts: mounting holes and fiducials.

    These are the ONLY footprints allowed on the board without a schematic
    symbol. link_schematic marks them "Not in schematic" (board_only), and
    check_parity fails any board-only footprint not named here, or any of
    these that carries a pad on a net -- so the attribute cannot be used to
    hide a circuit change. The ref scheme MIRRORS add_mounting_holes.hole_refs
    and add_fiducials' ref_prefix numbering -- keep the three identical.
    """
    out: dict[str, str] = {}
    mh = cfg.get("mounting_holes") or {}
    for i in range(1, len(mh.get("positions") or []) + 1):
        out[f"{mh.get('ref_prefix', 'H')}{i}"] = "mounting hole"
    fd = cfg.get("fiducials") or {}
    for i in range(1, len(fd.get("positions") or []) + 1):
        out[f"{fd.get('ref_prefix', 'FID')}{i}"] = "fiducial"
    return out
