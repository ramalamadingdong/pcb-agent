#!/usr/bin/env python3
"""generate_schematic — netlist.csv -> .kicad_sch, then prove it round-trips.

netlist.csv is the single source of truth for the board: it lists every
(net, refdes, pin) node. This pass turns it into a real KiCad schematic using
label-based connectivity — every pin gets a global label carrying its net
name, placed exactly on the pin's electrical connection point. No wires, so
there is no routing logic to get wrong and no way for the schematic to
disagree with the netlist.

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

from _lib import emit, fail, load_config, mechanical_parts, pass_parser  # noqa: E402

from kicad_tools.schematic.models.schematic import Schematic  # noqa: E402
from kicad_tools.schematic.registry import get_registry  # noqa: E402

# Stock symbol roots to fall back on when [libs] symbol_dirs is absent.
DEFAULT_SYMBOL_DIRS = [
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
]

# Schematic readability only — PCB placement is done later by the pipeline.
COLS, DX, DY = 9, 52, 42


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


def part_spec(cfg: dict, ref: str) -> dict:
    """Merge [parts.<REF>] over the matching [[part_rules]] prefix rule.

    An explicit per-refdes entry wins key by key, so a board can name one
    0805 capacitor without restating the symbol for every other one.

    MIRRORED in make_libs.py:part_spec — the two passes must agree about which
    footprint a refdes uses or the board vendors one geometry and the schematic
    references another. Keep them identical.
    """
    spec = dict((cfg.get("parts") or {}).get(ref) or {})
    prefix = "".join(ch for ch in ref if ch.isalpha())
    for rule in cfg.get("part_rules") or []:
        if rule.get("prefix") == prefix:
            for key, value in rule.items():
                if key != "prefix":
                    spec.setdefault(key, value)
            break
    return spec


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
        # Featured parts (the big ICs) get their own row with wide margins;
        # everything else goes on a coarse grid below.  This is schematic
        # readability only — PCB placement is done later by the pipeline.
        # ---------------------------------------------------------------------
        featured = [r for r in (sc.get("featured_refs") or []) if r in refs]
        missing_featured = [r for r in (sc.get("featured_refs") or []) if r not in refs]
        for r in missing_featured:
            log(f"  note: [schematic] featured_refs names {r}, which is not in the netlist")

        placed: dict[str, object] = {}

        def add(ref: str, x: float, y: float, full_value: bool) -> None:
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

            placed[ref] = sch.add_symbol(
                lib_id,
                x=x,
                y=y,
                ref=ref,
                value=value,
                footprint=f"{lib_name}:{fp_name}",
                properties=props or None,
                dnp=bool(spec.get("dnp")),
            )

        for i, ref in enumerate(featured):
            add(ref, 90 + i * 160, 110, full_value=True)

        rest = [r for r in refs if r not in featured]
        y0 = 220 if featured else 110
        for i, ref in enumerate(rest):
            add(ref, 40 + (i % COLS) * DX, y0 + (i // COLS) * DY, full_value=False)

        # ---------------------------------------------------------------------
        # Connectivity: one global label per node, on the pin's connection point.
        # ---------------------------------------------------------------------
        label_count = 0
        for ref in refs:
            sym = placed[ref]
            for net, pin in by_ref[ref]:
                if (ref, pin) in skip_nodes:
                    continue
                lib_pin = pin_alias.get((ref, pin), pin)
                try:
                    x, y = sym.pin_position(lib_pin)
                except Exception as exc:
                    fail(f"{ref}: no pin {lib_pin!r} on {sym.symbol_def.lib_id} ({exc})")
                # Point the label away from the symbol body so it does not overlap.
                rotation = 180 if x < sym.x else 0
                sch.add_global_label(
                    net, x, y, shape="passive", rotation=rotation, validate_connection=False
                )
                label_count += 1

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
