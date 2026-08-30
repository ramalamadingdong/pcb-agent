#!/usr/bin/env python3
"""make_libs — vendor every symbol and footprint this board needs into ONE project library.

`netlist.csv` names the parts. `board.toml` says which symbol and which
footprint each one uses. This pass resolves every one of those to a real file
and writes them into a single project-local library:

    <libs.out_dir>/<libs.name>.kicad_sym      generated symbols
    <libs.out_dir>/<libs.name>.pretty/*.kicad_mod   every footprint, vendored

Footprints are resolved from three sources, searched in this order:

    [libs] vendor_dir       hand-made and manufacturer-supplied .kicad_mod
    [libs] footprint_dirs   the stock KiCad footprint libraries
    [libs] jlc_cache_dir    an easyeda2kicad cache, <cache>/<LCSC>/<LCSC>.pretty/

plus footprints converted here from an Eagle .lbr ([[libs.eagle]]), and
symbols generated here from a pin table ([[libs.symbols]]) for the parts the
stock libraries do not carry.

Two reasons everything ends up in one project library rather than referencing
the installed libraries in place: kicad-tools resolves footprints from a
single footprint directory, and — more importantly — a board that is going to
be manufactured should pin its exact pad geometry rather than inherit whatever
the local KiCad install happens to ship.

Deriving a hard package from a vendor/OSHW source rather than hand-typing pad
coordinates is deliberate: a transposed pad on an LGA is invisible in review
and fatal in assembly.

Fails non-zero when a footprint the netlist asks for cannot be resolved to a
real library entry. A footprint that silently does not exist is a part that
silently does not land on the board.

Idempotent: the generated symbol library and every .kicad_mod in the project
.pretty are deleted before the run rebuilds them, so a re-run is byte-identical
and a footprint dropped from the netlist does not linger.

Config consumed: [libs], [[libs.symbols]], [[libs.eagle]], [parts],
[[part_rules]]. Relative paths in [libs] resolve against the directory holding
board.toml, not the working directory.

Not ported:
  - The source's three hand-written footprint emitters (a microSD socket, a
    chip antenna, a mounting hole) were literal transcribed pad tables for
    three specific parts, with no parameterisable content. A hand-made
    footprint belongs in vendor_dir as a .kicad_mod file, where this pass
    picks it up by name — not as Python that only ever builds one part.
  - The BOM-package lookup that chose an 0603 vs an 0805 land for a passive.
    The footprint now comes from [parts]/[[part_rules]], which says it
    outright instead of inferring it from a BOM string.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Running the script by path already puts this directory on sys.path; the
# insert only covers the isolated-mode / -P invocations where it does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import emit, fail, load_config, pass_parser  # noqa: E402

from kicad_tools.schematic.symbol_generator import (  # noqa: E402
    PinDef,
    PinSide,
    SymbolDef,
    generate_symbol_sexp,
)

# Stock library roots to fall back on when [libs] footprint_dirs is absent.
# Only the ones that exist are used.
DEFAULT_FOOTPRINT_DIRS = [
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
]


def log(msg: str) -> None:
    """Progress goes to stderr. Exactly one JSON line goes to stdout."""
    print(msg, file=sys.stderr)


# =============================================================================
# netlist + parts
# =============================================================================


def load_netlist_rows(path: Path) -> list[tuple[str, str, str]]:
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


def netlist_refs(rows: list[tuple[str, str, str]]) -> list[str]:
    """Distinct RefDes carrying a real net, in a stable order.

    Mirrors generate_schematic: a part appearing only on NC rows is not placed
    on the schematic, so it is not something this pass vendors a footprint for
    either. It is warned about rather than silently dropped.
    """
    live = {ref for net, ref, _ in rows if net != "NC"}
    nc_only = {ref for net, ref, _ in rows if net == "NC"} - live
    for ref in sorted(nc_only):
        log(f"  note: {ref} appears only on NC rows — not placed, not vendored")
    return sorted(live, key=lambda r: (r[0], len(r), r))


def part_spec(cfg: dict, ref: str) -> dict:
    """Merge [parts.<REF>] over the matching [[part_rules]] prefix rule.

    An explicit per-refdes entry wins key by key, so a board can name one
    0805 capacitor without restating the symbol for every other one.

    MIRRORED in generate_schematic.py:part_spec — the two passes must agree
    about which footprint a refdes uses or the board vendors one geometry and
    the schematic references another. Keep them identical.
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


def split_fp_ref(value: str) -> tuple[str | None, str]:
    """Split a footprint reference into (source library or None, name).

    "Connector_USB:USB_C_Receptacle_..." pins the source library.
    "USB_C_Receptacle_..." searches every root by name.
    """
    if ":" in value:
        lib, name = value.split(":", 1)
        return (lib or None), name
    return None, value


# =============================================================================
# symbols
# =============================================================================


def build_pins(pin_rows: list[dict], symbol_name: str) -> list[PinDef]:
    """Split a pin list down the middle: first half left, second half right.

    Keeps pin order monotonic down each side, which makes the generated symbol
    readable enough to review against the datasheet. A pin table that assigns
    its own `side` to any pin is passed through untouched instead.
    """
    pins = []
    for i, row in enumerate(pin_rows):
        if "number" not in row:
            fail(f"[[libs.symbols]] {symbol_name}: pin {i} has no `number`")
        try:
            pins.append(PinDef.from_dict(dict(row)))
        except ValueError as exc:  # unknown pin type / style
            fail(f"[[libs.symbols]] {symbol_name}: pin {row.get('number')}: {exc}")
    if not pins:
        fail(f"[[libs.symbols]] {symbol_name}: no pins")
    if any(p.side is not None for p in pins):
        return pins
    half = (len(pins) + 1) // 2
    for i, pin in enumerate(pins):
        pin.side = PinSide.LEFT if i < half else PinSide.RIGHT
    return pins


def build_symbols(cfg: dict, lib_name: str) -> list[SymbolDef]:
    out = []
    for entry in (cfg.get("libs") or {}).get("symbols") or []:
        name = entry.get("name")
        if not name:
            fail("[[libs.symbols]] entry has no `name`")
        fp = entry.get("footprint", "")
        if fp and ":" not in fp:
            # A bare footprint name means this project's library — everything
            # this pass vendors ends up there.
            fp = f"{lib_name}:{fp}"
        out.append(
            SymbolDef(
                name=name,
                pins=build_pins(entry.get("pins") or [], name),
                reference=entry.get("reference", "U"),
                value=entry.get("value", ""),
                footprint=fp,
                description=entry.get("description", ""),
                datasheet=entry.get("datasheet", ""),
                keywords=entry.get("keywords", ""),
            )
        )
    return out


def write_symbol_lib(path: Path, symbols: list[SymbolDef]) -> None:
    """Concatenate several single-symbol libraries into one multi-symbol lib.

    generate_symbol_sexp() emits a complete (kicad_symbol_lib ...) wrapper per
    symbol, so the wrapper is stripped from each and a single one re-applied.
    """
    header = None
    bodies = []
    for sym in symbols:
        sexp = generate_symbol_sexp(sym)
        start = sexp.index('\t(symbol "')
        if header is None:
            header = sexp[:start]
        # Drop the final ")\n" that closes kicad_symbol_lib
        body = sexp[start:].rstrip()
        if not body.endswith(")"):
            fail(f"{sym.name}: unexpected symbol library tail — generator output changed?")
        bodies.append(body[:-1].rstrip())

    path.write_text(header + "\n".join(bodies) + "\n)\n", encoding="utf-8")
    log(f"  symbols -> {path.name}  ({len(symbols)} symbols)")


# =============================================================================
# footprints
# =============================================================================


def eagle_package_to_kicad_mod(lbr: Path, package: str, name: str, descr: str) -> str:
    """Convert one Eagle <package> SMD pad set into a KiCad footprint.

    Eagle's Y axis points up and KiCad's points down, so Y is negated.  Eagle
    expresses a rotated pad with a rot="R90"/"R270" attribute rather than
    swapping dx/dy, so those are swapped here to get axis-aligned KiCad pads.
    """
    if not lbr.exists():
        fail(f"[[libs.eagle]] {name}: {lbr} not found")
    root = ET.parse(lbr).getroot()
    pkg = next((p for p in root.iter("package") if p.get("name") == package), None)
    if pkg is None:
        have = sorted(p.get("name") or "" for p in root.iter("package"))
        fail(f"{lbr.name}: no <package> named {package!r} (has: {', '.join(have)})")

    # Some Eagle packages name pads "P1".."P14" while the symbol numbers its pins
    # "1".."14".  KiCad matches symbol pin number to footprint pad number as a
    # string, so normalise a leading alpha prefix away or nothing connects.
    def pad_number(raw: str) -> str:
        m = re.fullmatch(r"[A-Za-z]+(\d+)", raw or "")
        return m.group(1) if m else raw

    pads = []
    xs, ys = [], []
    for smd in pkg.iter("smd"):
        x = float(smd.get("x"))
        y = -float(smd.get("y"))  # Eagle Y-up -> KiCad Y-down
        dx = float(smd.get("dx"))
        dy = float(smd.get("dy"))
        rot = (smd.get("rot") or "R0").upper()
        if rot in ("R90", "R270"):
            dx, dy = dy, dx
        pads.append((pad_number(smd.get("name")), x, y, dx, dy))
        xs += [x - dx / 2, x + dx / 2]
        ys += [y - dy / 2, y + dy / 2]

    if not pads:
        fail(f"no SMD pads found in {lbr.name}:{package}")

    # Courtyard 0.25 mm outside the pad extents; silk outline just outside that.
    cx0, cx1 = min(xs) - 0.25, max(xs) + 0.25
    cy0, cy1 = min(ys) - 0.25, max(ys) + 0.25
    sx0, sy0, sx1, sy1 = cx0 - 0.15, cy0 - 0.15, cx1 + 0.15, cy1 + 0.15

    safe_descr = descr.replace('"', "'")
    lines = [
        f'(footprint "{name}"',
        "  (version 20221018)",
        '  (generator "make_libs")',
        '  (layer "F.Cu")',
        f'  (descr "{safe_descr}")',
        "  (attr smd)",
        f'  (fp_text reference "REF**" (at 0 {sy0 - 1:.3f} 0) (layer "F.SilkS")'
        "    (effects (font (size 1 1) (thickness 0.15))))",
        f'  (fp_text value "{name}" (at 0 {sy1 + 1:.3f} 0) (layer "F.Fab")'
        "    (effects (font (size 1 1) (thickness 0.15))))",
    ]

    # Courtyard rectangle
    lines.append(
        f"  (fp_rect (start {cx0:.3f} {cy0:.3f}) (end {cx1:.3f} {cy1:.3f})"
        '    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))'
    )
    # Fabrication outline
    lines.append(
        f"  (fp_rect (start {cx0:.3f} {cy0:.3f}) (end {cx1:.3f} {cy1:.3f})"
        '    (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab"))'
    )
    # Silkscreen outline
    lines.append(
        f"  (fp_rect (start {sx0:.3f} {sy0:.3f}) (end {sx1:.3f} {sy1:.3f})"
        '    (stroke (width 0.12) (type solid)) (fill none) (layer "F.SilkS"))'
    )
    # Pin-1 marker.  Packages whose pads are named A/K, G/S/D and so on have no
    # pad "1" to key it off; those get no marker rather than a wrong one.
    p1 = next((p for p in pads if p[0] == "1"), None)
    if p1 is None:
        log(f"  note: {name} has no pad \"1\" — no pin-1 silk marker drawn")
    else:
        lines.append(
            f"  (fp_circle (center {p1[1]:.3f} {p1[2] - 0.9:.3f})"
            f" (end {p1[1] + 0.25:.3f} {p1[2] - 0.9:.3f})"
            '    (stroke (width 0.12) (type solid)) (fill solid) (layer "F.SilkS"))'
        )

    for num, x, y, dx, dy in pads:
        lines.append(
            f'  (pad "{num}" smd rect (at {x:.4f} {y:.4f}) (size {dx:.4f} {dy:.4f})'
            '    (layers "F.Cu" "F.Paste" "F.Mask"))'
        )

    lines.append(")")
    return "\n".join(lines) + "\n"


def reject_legacy_dialect(path: Path) -> None:
    """Refuse a footprint written in the legacy `(module ...)` dialect.

    kct copies a footprint file verbatim into the board, and kicad_tools then
    cannot read a legacy-dialect one back — the part loads in KiCad and looks
    placed, while every pass that resolves a reference reports it missing
    ("refs not on board"). EasyEDA/JLC exports are the usual source. Convert
    it to a modern (footprint ...) block and vendor that instead.
    """
    head = path.read_text(encoding="utf-8", errors="replace")[:4096].lstrip()
    if head.startswith("(module"):
        fail(
            f"{path}: legacy `(module ...)` footprint dialect. kct copies it into the "
            "board verbatim and kicad_tools then cannot resolve the reference. "
            "Convert it to a modern (footprint ...) block first."
        )


def footprint_candidates(root: Path, lib: str | None, name: str) -> list[Path]:
    """Every file in one search root that could be this footprint, best first."""
    hits: list[Path] = []
    if lib:
        p = root / f"{lib}.pretty" / f"{name}.kicad_mod"
        if p.is_file():
            hits.append(p)
    else:
        p = root / f"{name}.kicad_mod"
        if p.is_file():
            hits.append(p)
        # <root>/<Lib>.pretty/<name>.kicad_mod — a stock library tree.
        hits += sorted(root.glob(f"*.pretty/{name}.kicad_mod"))
        # <root>/<LCSC>/<LCSC>.pretty/<name>.kicad_mod — an easyeda2kicad cache.
        hits += sorted(root.glob(f"*/*.pretty/{name}.kicad_mod"))
    seen: set[Path] = set()
    return [p for p in hits if not (p in seen or seen.add(p))]


def resolve_footprint(spec: str, roots: list[Path]) -> Path:
    for root in roots:
        lib, name = split_fp_ref(spec)
        hits = footprint_candidates(root, lib, name)
        if not hits:
            continue
        if len(hits) > 1:
            bodies = {p.read_bytes() for p in hits}
            if len(bodies) > 1:
                listed = "\n    ".join(str(p) for p in hits)
                fail(
                    f"{spec}: {len(hits)} different footprints of that name under {root}:\n"
                    f"    {listed}\n"
                    "  Qualify it as Library:Name in board.toml so the geometry is not a coin toss."
                )
        return hits[0]
    listed = ", ".join(str(r) for r in roots) or "(no search roots configured)"
    fail(f"footprint not found: {spec} — searched {listed}")


# =============================================================================
# main
# =============================================================================


def resolve_dirs(cfg_dir: Path, libs: dict) -> list[Path]:
    """Search roots, highest priority first: vendor, stock, JLC cache."""

    def rel(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else (cfg_dir / q)

    roots: list[Path] = []
    if libs.get("vendor_dir"):
        roots.append(rel(libs["vendor_dir"]))

    configured = libs.get("footprint_dirs")
    if configured:
        roots += [rel(p) for p in configured]
    else:
        env = os.environ.get("KICAD_FOOTPRINT_DIR")
        roots += [Path(p) for p in ([env] if env else []) + DEFAULT_FOOTPRINT_DIRS]

    if libs.get("jlc_cache_dir"):
        roots.append(rel(libs["jlc_cache_dir"]))

    missing = [r for r in roots if not r.is_dir()]
    for r in missing:
        log(f"  note: search root does not exist, skipped: {r}")
    return [r for r in roots if r.is_dir()]


def main() -> int:
    ap = pass_parser("make_libs", board=False, netlist=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        fail(f"{args.config}: config not found or empty")
    cfg_dir = Path(args.config).resolve().parent

    libs = cfg.get("libs") or {}
    lib_name = libs.get("name")
    if not lib_name:
        fail("board.toml needs [libs] name — the project library everything is vendored into")

    out_dir = Path(libs.get("out_dir", "."))
    if not out_dir.is_absolute():
        out_dir = cfg_dir / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    sym_out = out_dir / f"{lib_name}.kicad_sym"
    fp_out = out_dir / f"{lib_name}.pretty"

    roots = resolve_dirs(cfg_dir, libs)

    rows = load_netlist_rows(Path(args.netlist))
    refs = netlist_refs(rows)

    # --- what this board needs -------------------------------------------
    required: dict[str, list[str]] = {}  # footprint spec -> refs asking for it
    for ref in refs:
        spec = part_spec(cfg, ref)
        fp = spec.get("footprint")
        if not fp:
            fail(
                f"no footprint mapping for {ref} — add [parts.{ref}] footprint = \"...\" "
                "or a [[part_rules]] entry for its prefix"
            )
        required.setdefault(fp, []).append(ref)

    # Fiducials, mounting holes and other bare pads are not netlist objects,
    # so nothing else in the pipeline asks for their footprints.
    for fp in libs.get("extra_footprints") or []:
        required.setdefault(fp, []).append("(extra)")

    # --- strip before rebuilding ------------------------------------------
    removed = 0
    if sym_out.exists():
        sym_out.unlink()
        removed += 1
    fp_out.mkdir(exist_ok=True)
    for old in sorted(fp_out.glob("*.kicad_mod")):
        old.unlink()
        removed += 1

    # --- symbols -----------------------------------------------------------
    symbols = build_symbols(cfg, lib_name)
    if symbols:
        write_symbol_lib(sym_out, symbols)
    else:
        log("  symbols -> none declared ([[libs.symbols]] empty); stock symbols only")

    # --- footprints converted from an Eagle library ------------------------
    vendor_dir = libs.get("vendor_dir")
    vendor_root = (
        (Path(vendor_dir) if Path(vendor_dir).is_absolute() else cfg_dir / vendor_dir)
        if vendor_dir
        else cfg_dir
    )
    generated: list[str] = []
    for job in libs.get("eagle") or []:
        for key in ("lbr", "package", "name"):
            if not job.get(key):
                fail(f"[[libs.eagle]] entry missing `{key}`")
        lbr = Path(job["lbr"])
        if not lbr.is_absolute():
            lbr = vendor_root / lbr
        text = eagle_package_to_kicad_mod(
            lbr, job["package"], job["name"], job.get("description", "")
        )
        (fp_out / f"{job['name']}.kicad_mod").write_text(text, encoding="utf-8")
        generated.append(job["name"])
        log(f"  footprint -> {job['name']}.kicad_mod ({text.count('(pad ')} pads, from {lbr.name})")

    # Two different source libraries can carry the same footprint name with
    # different pad geometry.  Vendoring both writes one file twice and the
    # survivor is whichever ran last — so refuse the ambiguity outright.
    by_name: dict[str, str] = {}
    for spec in sorted(required):
        _, name = split_fp_ref(spec)
        if name in by_name and by_name[name] != spec:
            fail(
                f"two different footprint references resolve to {name}.kicad_mod: "
                f"{by_name[name]!r} and {spec!r} — they cannot both be vendored under one name"
            )
        by_name[name] = spec

    # --- everything else, resolved and copied verbatim ---------------------
    vendored: list[str] = []
    for spec in sorted(required):
        _, name = split_fp_ref(spec)
        if name in generated:
            continue  # produced from the Eagle library above
        src = resolve_footprint(spec, roots)
        reject_legacy_dialect(src)
        shutil.copyfile(src, fp_out / f"{name}.kicad_mod")
        vendored.append(name)
    log(f"  footprints -> {len(vendored)} vendored, {len(generated)} generated")

    emit(
        "make_libs",
        library=lib_name,
        symbol_lib=str(sym_out) if symbols else None,
        symbols=len(symbols),
        footprint_lib=str(fp_out),
        footprints=len(vendored) + len(generated),
        vendored=vendored,
        generated=generated,
        refs=len(refs),
        stripped=removed,
        search_roots=[str(r) for r in roots],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
