#!/usr/bin/env python3
"""generate_schematic — netlist.csv -> .kicad_sch, then prove it round-trips.

netlist.csv is the single source of truth for the board: it lists every
(net, refdes, pin) node. This pass turns it into a real KiCad schematic using
label-based connectivity — every pin gets a global label carrying its net
name, placed exactly on the pin's electrical connection point. No routing,
so there is no routing logic to get wrong and no way for the schematic to
disagree with the netlist.

The one exception is a Direct-tagged part (see direct_connect.py): the board
puts its pad on its target's pad, so the schematic draws it the same way --
a straight wire out of the target pin, the part hanging off it at a
junction, the net's label at the wire's far end. Straight runs only, placed
by a collision search, and the same round-trip below proves them.

Layout is a packer, not a grid: each part plus its labels (and anything
hung on it) is one block, and blocks are packed tallest first into
`[schematic] pack_width_mm` (sch_layout.skyline_pack). Featured refs go
first.

The generated schematic is then verified by round-tripping: extract_netlist()
re-reads the .kicad_sch and the result is diffed against netlist.csv. A
mismatch fails the build rather than producing a plausible-looking board.

THAT DIFF IS THE MOST IMPORTANT CHECK IN THE PIPELINE. A label a fraction of
a millimetre off a pin produces a schematic that looks perfect and carries no
net: ERC reads it as an intentional floating label, and DRC never looks at
schematics at all. Nothing downstream will tell you.

Config consumed: [libs] (name, out_dir, symbol_dirs), [schematic], [parts],
[[part_rules]], [[pin_alias]]. Relative paths resolve against the directory
holding board.toml.

Not ported:
  - The BOM "Footprint" column that chose an 0603 vs an 0805 land for a
    passive. The footprint comes from [parts]/[[part_rules]] now, which says
    it outright rather than inferring it from a package string.
  - The board-specific PARTS / PIN_ALIAS / SKIP_NODES / PWR_FLAG tables and
    the hard-coded featured refdes row; all four are board.toml now.
"""

from __future__ import annotations

import contextlib
import csv
import math
import os
import random
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path

# Running the script by path already puts this directory on sys.path; the
# insert only covers the isolated-mode / -P invocations where it does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import emit, fail, load_config, mechanical_parts, part_spec, pass_parser  # noqa: E402
import direct_connect  # noqa: E402
import sch_layout as sl  # noqa: E402

from kicad_tools.schematic.models.schematic import Schematic  # noqa: E402
from kicad_tools.schematic.registry import get_registry  # noqa: E402

# Stock symbol roots to fall back on when [libs] symbol_dirs is absent.
DEFAULT_SYMBOL_DIRS = [
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
]

# Schematic readability only — PCB placement is done later by the pipeline.
# Blocks are packed into this width (A3 landscape less its frame); the paper
# grows to fit whatever height that takes.
PACK_WIDTH_MM = 370.0


def log(msg: str) -> None:
    """Progress goes to stderr. Exactly one JSON line goes to stdout."""
    print(msg, file=sys.stderr)


@contextlib.contextmanager
def deterministic_uuids(seed: str):
    """Seed the generator's uuid4 so a re-run writes a byte-identical file.

    Every schematic element gets a random uuid4, so two runs over an unchanged
    netlist produce two files that differ on almost every line — which makes a
    diff useless for answering the one question worth asking after a rebuild:
    did anything about the design actually change? A pass must be idempotent
    (CONTRACT.md), so uuid4 is seeded from a stable string for the duration of
    the write and restored afterwards.
    """
    rng = random.Random(seed)
    real = uuid.uuid4
    uuid.uuid4 = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)
    try:
        yield
    finally:
        uuid.uuid4 = real


def register_libraries(project_lib_dir: Path, symbol_dirs: list[Path]) -> None:
    """Register the stock and project symbol libraries explicitly.

    kicad-tools discovers symbol libraries from a fixed list of platform paths
    that will not include every install, so both the stock KiCad libraries and
    this project's own .kicad_sym have to be registered by hand.
    """
    reg = get_registry()
    for d in [project_lib_dir, *symbol_dirs]:
        if d.exists() and d not in reg.lib_paths:
            reg.lib_paths.insert(0, d)
    if not any((d / "Device.kicad_sym").exists() for d in reg.lib_paths):
        fail(
            "Stock KiCad symbol libraries not found. Set [libs] symbol_dirs in board.toml "
            "(or KICAD_SYMBOL_DIR) to the directory containing Device.kicad_sym."
        )


# =============================================================================
# inputs
# =============================================================================


def read_rows(path: Path) -> list[tuple[str, str, str]]:
    """[(net, ref, pin)] from netlist.csv, dropping comments and the header.

    Net/RefDes/Pin never contain a comma, so the first three fields survive a
    plain split even when the trailing Note column is quoted.
    """
    if not path.exists():
        fail(f"{path}: netlist not found")
    rows: list[tuple[str, str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Net,"):
            continue
        f = line.split(",")
        if len(f) < 3 or not f[0] or not f[1] or not f[2]:
            fail(f"{path}:{lineno}: expected at least Net,RefDes,Pin — got {line!r}")
        rows.append((f[0].strip(), f[1].strip(), f[2].strip()))
    if not rows:
        fail(f"{path}: no netlist rows")
    return rows


def load_bom(path: Path, sc: dict) -> dict[str, dict]:
    """ref -> {value, properties} from an optional assembly BOM.

    Column names are configurable because every distributor's export uses its
    own. One row may cover several designators, separated by spaces or commas.
    """
    if not path.exists():
        fail(f"{path}: [schematic] bom not found")
    desig_col = sc.get("bom_designator_column", "Designator")
    value_col = sc.get("bom_value_column", "Comment")
    prop_cols: dict[str, str] = sc.get("bom_property_columns") or {}

    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or desig_col not in reader.fieldnames:
            fail(
                f"{path}: no {desig_col!r} column "
                f"(has: {', '.join(reader.fieldnames or [])}). "
                "Set [schematic] bom_designator_column."
            )
        for row in reader:
            if not row.get(desig_col):
                continue
            props = {k: (row.get(col) or "").strip() for k, col in prop_cols.items()}
            props = {k: v for k, v in props.items() if v}
            for ref in re.split(r"[,\s]+", row[desig_col].strip()):
                if ref:
                    out[ref] = {"value": (row.get(value_col) or "").strip(), "properties": props}
    return out


def short_value(comment: str) -> str:
    """Turn a BOM comment into a schematic-sized value string."""
    first = comment.split("(")[0].strip()
    words = first.split()
    for w in words:
        if any(ch.isdigit() for ch in w) and (
            w.endswith(("R", "k", "M", "F", "uF", "nF", "pF", "Hz", "kHz")) or w[0].isdigit()
        ):
            return w
    return first[:24]


def board_only_notes(cfg: dict) -> list[str]:
    """One printed line per kind of footprint the board carries without a
    symbol: the mounting holes and fiducials board.toml declares.

    They are real copper and drill, but not circuit parts, so they are not
    drawn as symbols; the board marks them "Not in schematic" and
    check_parity fails if any of them ever carries a net. Saying so on the
    sheet means a reviewer looking for H1 finds the reason instead of a gap.
    """
    kinds: dict[str, list[str]] = {}
    for ref, what in mechanical_parts(cfg).items():
        kinds.setdefault(what, []).append(ref)
    return [f"Board only, not drawn here ({what}s, no net): {', '.join(refs)}"
            for what, refs in kinds.items()]


# =============================================================================
# layout
# =============================================================================

# Body graphics in a library symbol. Pins are measured separately, and pin
# and property positions are `(at ...)` nodes, which this does not match.
_GFX_XY = re.compile(r"\((?:xy|start|end|center|mid)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def _place_local(sym, lx: float, ly: float) -> tuple[float, float]:
    """Library (Y up) point -> sheet point, the transform pin_position uses."""
    rad = math.radians(sym.rotation)
    rx = lx * math.cos(rad) - ly * math.sin(rad)
    ry = lx * math.sin(rad) + ly * math.cos(rad)
    return sym.x + rx, sym.y - ry


def outward(sym, lib_pin: str) -> tuple[int, int]:
    """Unit direction a pin points AWAY from its body, on the sheet.

    A library pin's angle points from its connection end toward the body;
    rotate that by the instance rotation, flip Y for the sheet, reverse it.
    """
    pin = sym.find_pin(lib_pin)
    a = math.radians(pin.angle + sym.rotation)
    return (-round(math.cos(a)), round(math.sin(a)))


def face(sym, lib_pin: str, want: tuple[int, int]) -> bool:
    """Rotate `sym` so `lib_pin` points `want`; False if no rotation does."""
    for rot in (0, 90, 180, 270):
        sym.rotation = rot
        if outward(sym, lib_pin) == want:
            return True
    return False


def symbol_box(sym) -> sl.Box:
    """Everything a placed symbol draws: pins, body outline, ref and value."""
    pts = [sym.pin_position(p.number or p.name) for p in sym.symbol_def.pins]
    raw = sym.symbol_def.raw_sexp or ""
    pts += [_place_local(sym, float(a), float(b)) for a, b in _GFX_XY.findall(raw)]
    if not pts:
        pts = [(sym.x, sym.y)]
    box = sl.grow(sl.union((x, y, x, y) for x, y in pts), 1.27)
    # generate writes Reference at y-5.08 and Value at y-2.54, centred on x.
    return sl.union([box, sl.text_box(sym.x, sym.y - 5.08, sym.reference),
                     sl.text_box(sym.x, sym.y - 2.54, sym.value or "")])


def pin_labels(sym, pins: list[tuple[str, str]], along_pin: bool = False):
    """Global-label ops and boxes for (net, lib_pin) on a placed symbol.

    Labels point left or right, away from the body -- vertical labels on a
    vertical passive would run through its reference text, which KiCad
    places above the symbol whatever its rotation. `along_pin` points them
    the way the pin does instead -- down, under a part hung below a wire --
    except upward, which is exactly where that text is.
    """
    ops, boxes = [], []
    for net, lib_pin in pins:
        x, y = sym.pin_position(lib_pin)
        d = outward(sym, lib_pin) if along_pin else (0, -1)
        if d[0] == 0 and not (along_pin and d == (0, 1)):
            d = (-1, 0) if x < sym.x else (1, 0)
        ops.append(("label", net, x, y, sl.DIRS[d]))
        boxes.append(sl.label_box(x, y, d, net))
    return ops, boxes


def hang(target_sym, t_pin: str, net: str, sats: list, occupied: list[sl.Box],
         policy: str = "nearest"):
    """Draw Direct satellites off a target pin: pin -> wire -> part(s) -> label.

    `sats` is [(sym, tagged lib pin, [(net, lib_pin), ...] other pins)]. The
    wire leaves the target pin in its outward direction; each satellite hangs
    perpendicular off it, its tagged pin ON the wire (with a junction), and
    the net's label sits at the wire's far end. The run length and the side
    the parts hang on are searched ("tetris") until nothing drawn collides
    with anything already in `occupied`. `policy` orders that search:
    "nearest" takes the shortest run on either side; "down" and "up" exhaust
    one side (down, or right, for "down") before trying the other. Returns
    (ops, boxes) or None.
    """
    tx, ty = target_sym.pin_position(t_pin)
    d = outward(target_sym, t_pin)
    perps = [(-d[1], d[0]), (d[1], -d[0])]
    perps.sort(key=lambda n: (n[1] < 0, n[0] < 0))  # hang down / right first
    if policy == "up":
        perps.reverse()
    runs = range(2, 80)
    tries = ([(k, n) for k in runs for n in perps] if policy == "nearest"
             else [(k, n) for n in perps for k in runs])
    for steps, n in tries:
        want = (-n[0], -n[1])  # the tagged pin must point back at the wire
        ops, boxes = [], []
        cx, cy = sl.r2(tx + d[0] * steps * sl.GRID), sl.r2(ty + d[1] * steps * sl.GRID)
        joints = []
        ok = True
        for sym, pin, others in sats:
            if not face(sym, pin, want):
                ok = False
                break
            sym.x = sym.y = 0.0
            px, py = sym.pin_position(pin)
            # Slide along the wire until clear of the parts before it.
            while True:
                sym.x, sym.y = sl.r2(cx - px), sl.r2(cy - py)
                sb = symbol_box(sym)
                lops, lboxes = pin_labels(sym, others, along_pin=True)
                if not any(sl.hits(a, b) for a in (sb, *lboxes) for b in boxes):
                    break
                cx, cy = sl.r2(cx + d[0] * sl.GRID), sl.r2(cy + d[1] * sl.GRID)
            ops += lops
            boxes += [sb, *lboxes]
            joints.append((cx, cy))
            # Next part: past this one's extent along the wire.
            along = (sb[2] - cx) if d[0] > 0 else (cx - sb[0]) if d[0] < 0 else \
                    (sb[3] - cy) if d[1] > 0 else (cy - sb[1])
            step = sl.snap_up(max(along, 0) + sl.GRID)
            cx, cy = sl.r2(cx + d[0] * step), sl.r2(cy + d[1] * step)
        if not ok:
            continue
        end = (cx, cy)
        pts = [(tx, ty), *joints, end]
        for a, b in zip(pts, pts[1:]):
            ops.append(("wire", a, b))
        for j in joints:
            ops.append(("junction", *j))
        ops.append(("label", net, *end, sl.DIRS[d]))
        label = sl.label_box(*end, d, net)
        # The wire starts on the target's own pin, inside its box: test
        # it from one grid step out.
        wire = sl.wire_box((tx + d[0] * sl.GRID, ty + d[1] * sl.GRID), end)
        new = [*boxes, label, wire]
        if any(sl.hits(a, b) for a in new for b in occupied):
            continue
        return ops, [*boxes, label, wire]
    return None


# =============================================================================
# main
# =============================================================================


def main() -> int:
    ap = pass_parser("generate_schematic", board=False, schematic=True, netlist=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        fail(f"{args.config}: config not found or empty")
    cfg_dir = Path(args.config).resolve().parent

    def rel(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else (cfg_dir / q)

    libs = cfg.get("libs") or {}
    lib_name = libs.get("name")
    if not lib_name:
        fail("board.toml needs [libs] name — the project library make_libs vendors into")
    project_lib_dir = rel(libs.get("out_dir", "."))

    sc = cfg.get("schematic") or {}
    env_sym = os.environ.get("KICAD_SYMBOL_DIR")
    symbol_dirs = [rel(p) for p in (libs.get("symbol_dirs") or [])] or [
        Path(p) for p in ([env_sym] if env_sym else []) + DEFAULT_SYMBOL_DIRS
    ]
    register_libraries(project_lib_dir, symbol_dirs)

    bom = load_bom(rel(sc["bom"]), sc) if sc.get("bom") else {}

    rows = read_rows(Path(args.netlist))
    nodes = [(net, ref, pin) for net, ref, pin in rows if net != "NC"]
    nc_pins = [(ref, pin) for net, ref, pin in rows if net == "NC"]

    # Pin aliases: netlist.csv pin id -> library pin id.  A symbol that merges
    # pins the netlist names separately (a connector shell tabbed twice, a card
    # socket numbering its shield) needs the netlist id mapped onto the library
    # id, and every alias after the first onto the same library pin marked
    # `skip` — the label is already there and a second one would duplicate the
    # node in the round-trip.
    pin_alias: dict[tuple[str, str], str] = {}
    skip_nodes: set[tuple[str, str]] = set()
    for entry in cfg.get("pin_alias") or []:
        for key in ("ref", "pin"):
            if not entry.get(key):
                fail(f"[[pin_alias]] entry missing `{key}`")
        node = (entry["ref"], str(entry["pin"]))
        if entry.get("lib_pin"):
            pin_alias[node] = str(entry["lib_pin"])
        if entry.get("skip"):
            skip_nodes.add(node)

    refs = sorted({ref for _, ref, _ in nodes}, key=lambda r: (r[0], len(r), r))
    by_ref: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for net, ref, pin in nodes:
        by_ref[ref].append((net, pin))

    out = Path(args.schematic)
    out.parent.mkdir(parents=True, exist_ok=True)

    seed = str(sc.get("uuid_seed") or (cfg.get("board") or {}).get("name") or lib_name)
    with deterministic_uuids(seed):
        sch = Schematic(
            title=sc.get("title") or (cfg.get("board") or {}).get("name") or out.stem,
            date=str(sc.get("date", "")),
            revision=str(sc.get("revision", "A")),
            company=sc.get("company", ""),
            comment1=sc.get("comment1", ""),
            comment2=sc.get("comment2", "Generated from netlist.csv - do not hand-edit"),
        )

        # ---------------------------------------------------------------------
        # Placement
        #
        # Every part is a block: its symbol and the labels on its pins. A part
        # tagged Direct in netlist.csv is not a block of its own -- it hangs
        # off a short wire from the pin it touches on the board, inside its
        # target's block (hang()). The blocks are then packed onto the sheet
        # tallest first, skyline style (sch_layout.skyline_pack), featured
        # parts ahead of everything. Readability only; PCB placement is done
        # later by the pipeline. Connectivity is still one label per net at
        # each block, and the round-trip below proves every node survived.
        # ---------------------------------------------------------------------
        featured = [r for r in (sc.get("featured_refs") or []) if r in refs]
        missing_featured = [r for r in (sc.get("featured_refs") or []) if r not in refs]
        for r in missing_featured:
            log(f"  note: [schematic] featured_refs names {r}, which is not in the netlist")

        placed: dict[str, object] = {}

        def add(ref: str, full_value: bool) -> None:
            spec = part_spec(cfg, ref)
            if not spec.get("symbol"):
                fail(
                    f"no symbol mapping for {ref} — add [parts.{ref}] symbol = \"...\" "
                    "or a [[part_rules]] entry for its prefix"
                )
            if not spec.get("footprint"):
                fail(
                    f"no footprint mapping for {ref} — add [parts.{ref}] footprint = \"...\" "
                    "or a [[part_rules]] entry for its prefix"
                )
            # A bare id means this project's library: make_libs vendors every
            # footprint into one library, so the board resolves them all there.
            lib_id = spec["symbol"] if ":" in spec["symbol"] else f"{lib_name}:{spec['symbol']}"
            fp_name = spec["footprint"].split(":", 1)[-1]

            b = bom.get(ref, {})
            bom_value = b.get("value") or ""
            value = spec.get("value") or (
                bom_value if full_value else short_value(bom_value)
            ) or ref
            props = dict(b.get("properties") or {})
            props.update(spec.get("properties") or {})

            # Placed at the origin; the packer moves it. Position and
            # rotation are read at write time, so moving is safe.
            placed[ref] = sch.add_symbol(
                lib_id,
                x=0,
                y=0,
                ref=ref,
                value=value,
                footprint=f"{lib_name}:{fp_name}",
                properties=props or None,
                dnp=bool(spec.get("dnp")),
            )

        for ref in refs:
            add(ref, full_value=ref in featured)

        def lib_pins(ref: str) -> list[tuple[str, str]]:
            return [(net, pin_alias.get((ref, pin), pin)) for net, pin in by_ref[ref]
                    if (ref, pin) not in skip_nodes]

        # Direct satellites, grouped by (target ref, target lib pin).
        anchors = direct_connect.anchor_set(cfg)
        hangs: dict[tuple[str, str], list] = defaultdict(list)
        for t in direct_connect.load_tags(Path(args.netlist), cfg):
            tref, tpin = direct_connect.preferred(t, anchors)
            if (t.ref, t.pin) in skip_nodes or (tref, tpin) in skip_nodes:
                continue
            hangs[(tref, pin_alias.get((tref, tpin), tpin))].append(
                (t.ref, pin_alias.get((t.ref, t.pin), t.pin), t.net))
        satellites = {s[0] for v in hangs.values() for s in v}

        blocks: dict[str, tuple[list[str], list[tuple], sl.Box]] = {}
        fallback: list[str] = []
        for ref in refs:
            if ref in satellites:
                continue
            sym = placed[ref]
            t_pins = {p for (r, p) in hangs if r == ref}
            base_ops, base = pin_labels(sym, [(n, p) for n, p in lib_pins(ref) if p not in t_pins])
            base = [symbol_box(sym), *base]

            def pin_key(p: str, sym=sym):
                x, y = sym.pin_position(p)
                return (y if outward(sym, p)[0] else x, p)

            def attempt(order: list[str], policy: str, sym=sym, ref=ref):
                """Hang every target pin in `order`; (ops, boxes, members, missed)."""
                ops, occupied, members, missed = list(base_ops), list(base), [ref], []
                for t_pin in order:
                    group = sorted(hangs[(ref, t_pin)])
                    sats = [(placed[s], sp, [(n, p) for n, p in lib_pins(s) if p != sp])
                            for s, sp, _ in group]
                    got = hang(sym, t_pin, group[0][2], sats, occupied, policy)
                    if got is None:
                        missed.append(t_pin)
                        lops, lboxes = pin_labels(sym, [(group[0][2], t_pin)])
                        ops += lops
                        occupied += lboxes
                        continue
                    ops += got[0]
                    occupied += got[1]
                    members += [s for s, _, _ in group]
                return ops, occupied, members, missed

            # A greedy run can box itself in: an early pin takes the cheap
            # spot a later pin needed. So try a few arrangements -- pins taken
            # bottom-up or top-down, each side-preference -- keep the first
            # that hangs everything, else the one that hangs the most.
            # Deterministic; re-run the winner last so the satellites' poses
            # are its poses.
            by_pos = sorted(t_pins, key=pin_key)
            plans = [(o, pol) for o in (by_pos[::-1], by_pos) for pol in ("down", "up", "nearest")]
            best = None
            for order, pol in plans:
                res = attempt(order, pol)
                if best is None or len(res[3]) < len(best[1][3]):
                    best = ((order, pol), res)
                if not res[3]:
                    break
            ops, occupied, members, missed = attempt(*best[0]) if t_pins else (base_ops, base, [ref], [])
            for t_pin in missed:
                group = sorted(hangs[(ref, t_pin)])
                # Readability, not connectivity: draw it the plain way.
                log(f"  WARNING {ref}.{t_pin}: no collision-free spot to hang "
                    f"{[s for s, _, _ in group]} -- drawn as separate blocks")
                fallback += [s for s, _, _ in group]
            blocks[ref] = (members, ops, sl.union(occupied))
        for ref in fallback:
            sym = placed[ref]
            sym.rotation, sym.x, sym.y = 0, 0.0, 0.0
            ops, boxes = pin_labels(sym, lib_pins(ref))
            blocks[ref] = ([ref], ops, sl.union([symbol_box(sym), *boxes]))

        def size(k: str) -> tuple[float, float]:
            b = blocks[k][2]
            return b[2] - b[0], b[3] - b[1]

        order = featured + sorted((k for k in blocks if k not in featured),
                                  key=lambda k: (-size(k)[1], -size(k)[0], k[0], len(k), k))
        width = float(sc.get("pack_width_mm", PACK_WIDTH_MM))
        offsets = sl.skyline_pack([(k, *size(k)) for k in order if k in blocks], width)

        # Below the notes and the PWR_FLAG row.
        n_notes = len(sc.get("notes") or [1]) + len(board_only_notes(cfg))
        ox, oy = 25.4, sl.snap_up(max(76.2, 40 + 10 * n_notes))
        label_count = 0
        for k, (members, ops, box) in blocks.items():
            px, py = offsets[k]
            # Whole grid steps, so every pin stays on the connection grid.
            dx, dy = sl.snap_up(ox + px - box[0]), sl.snap_up(oy + py - box[1])
            for m in members:
                placed[m].x, placed[m].y = sl.r2(placed[m].x + dx), sl.r2(placed[m].y + dy)
            for op in ops:
                if op[0] == "label":
                    _, net, x, y, rot = op
                    sch.add_global_label(net, sl.r2(x + dx), sl.r2(y + dy), shape="passive",
                                         rotation=rot, validate_connection=False)
                    label_count += 1
                elif op[0] == "wire":
                    (ax, ay), (bx, by) = op[1], op[2]
                    sch.add_wire((sl.r2(ax + dx), sl.r2(ay + dy)), (sl.r2(bx + dx), sl.r2(by + dy)),
                                 warn_on_collision=False)
                else:
                    sch.add_junction(sl.r2(op[1] + dx), sl.r2(op[2] + dy))
        hung = sorted(satellites - set(fallback))
        if hung:
            log(f"  direct        : {', '.join(hung)} drawn on their target pins")

        # ---------------------------------------------------------------------
        # No-connect markers on every pin netlist.csv marks NC.
        #
        # Without these ERC reports "Pin not connected" and a reviewer cannot
        # tell a deliberate manufacturer no-connect from a pin someone forgot to
        # wire.  Making the intent explicit is the whole point.
        # ---------------------------------------------------------------------
        nc_count = 0
        for ref, pin in nc_pins:
            sym = placed.get(ref)
            if sym is None:
                continue
            try:
                x, y = sym.pin_position(pin_alias.get((ref, pin), pin))
            except Exception as exc:
                fail(f"{ref}: no pin {pin!r} to mark no-connect ({exc})")
            sch.add_no_connect(x, y)
            nc_count += 1

        # ---------------------------------------------------------------------
        # PWR_FLAGs so ERC knows a rail is externally driven.
        #
        # Only list the rails ERC cannot already see a driver for — one arriving
        # from a connector, one passing through a passive.  A rail whose source
        # pin is declared a power OUTPUT must NOT be listed: flagging it too
        # trips ERC's "two power outputs connected" rule.
        # ---------------------------------------------------------------------
        pwr_nets = list(sc.get("pwr_flag_nets") or [])
        known = {net for net, _, _ in nodes}
        for net in pwr_nets:
            if net not in known:
                fail(f"[schematic] pwr_flag_nets names {net!r}, which is not a net in the netlist")
        for i, net in enumerate(pwr_nets):
            x, y = 40 + i * 28, 60
            sch.add_pwr_flag(x, y)
            sch.add_global_label(net, x, y, shape="passive", validate_connection=False)

        notes = list(sc.get("notes") or ["Generated from netlist.csv - do not hand-edit"])
        notes += board_only_notes(cfg)
        for i, line in enumerate(notes):
            sch.add_text(line, 40, 30 + i * 10)

        sch.write(out)

    log(f"Wrote {out.name}")
    log(f"  symbols       : {len(sch.symbols)}")
    log(f"  global labels : {label_count}")
    log(f"  wires         : {len(sch.wires)}")
    log(f"  nets          : {len({n for n, _, _ in nodes})}")

    matched = verify(out, placed, nodes, pin_alias, skip_nodes)

    emit(
        "generate_schematic",
        schematic=str(out),
        symbols=len(sch.symbols),
        global_labels=label_count,
        no_connects=nc_count,
        pwr_flags=len(pwr_nets),
        nets=len({n for n, _, _ in nodes}),
        round_trip_nodes=matched,
        sidecars=[str(out.parent / "sym-lib-table"), str(out.parent / "kicad_tools_pwr.kicad_sym")],
    )
    return 0


def verify(
    out: Path,
    placed: dict,
    nodes: list[tuple[str, str, str]],
    pin_alias: dict[tuple[str, str], str],
    skip_nodes: set[tuple[str, str]],
) -> int:
    """Re-read the written schematic and diff its connectivity against netlist.csv.

    KiCad reports pins by number, while netlist.csv uses the datasheet's pin
    name where that reads better (LED A/K, FET G/S/D).  Both are resolved
    through the symbol definition so the comparison is apples-to-apples.
    """
    from kicad_tools.schematic.models.schematic import Schematic as _Sch

    expected: set[tuple[str, str, str]] = set()
    for net, ref, pin in nodes:
        if (ref, pin) in skip_nodes:
            continue
        lib_pin = pin_alias.get((ref, pin), pin)
        expected.add((net, ref, placed[ref].find_pin(lib_pin).number))

    sch = _Sch.load(out)
    actual = {
        (net, p.symbol_ref, str(p.pin))
        for net, pins in sch.extract_netlist().items()
        for p in pins
        # Singleton auto-named nets are the deliberate no-connects.
        if not net.startswith("Net-(")
    }

    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        log("\nNETLIST ROUND-TRIP FAILED")
        for n in sorted(missing)[:20]:
            log(f"  missing from schematic: {n}")
        for n in sorted(extra)[:20]:
            log(f"  unexpected in schematic: {n}")
        fail(
            f"{out}: round-trip diff found {len(missing)} missing and {len(extra)} unexpected "
            "nodes. The schematic does not carry the netlist — do not build a board from it."
        )

    log(f"  round-trip    : OK ({len(expected)} nodes match netlist.csv exactly)")
    return len(expected)


if __name__ == "__main__":
    sys.exit(main())
