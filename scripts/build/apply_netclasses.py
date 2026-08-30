#!/usr/bin/env python3
"""apply_netclasses — pin this board's power nets to the power netclass BY NAME.

The root cause this pass exists for: a freshly generated .kicad_pro carries
generic power-class patterns (VCC*, VDD*, +*V, V_*) that can match NONE of a
given board's actual power net names. Every one of them then falls into
Default and routes at the default width — on the board this was written for,
a 500 mA charge path routed at 0.2 mm, and nothing anywhere reported a
problem. The .kicad_pro is what pcbnew (and therefore the freerouting DSN
export) resolves widths from, so a net-class map living anywhere else does not
reach the router.

So the assignment is pinned explicitly: every net name in netlist.csv matched
by a [nets.power] pattern is written into the .kicad_pro as its own literal
netclass pattern, and the class track width is set to
[nets.power] class_width_mm.

A declared pattern that matches no net in netlist.csv FAILS the pass. That is
the whole defect restated — a power rule that silently applies to nothing —
and the generic defaults are exactly what it looks like. Patterns are
anchored, so `VBAT` does not also grab a `VBAT_SENSE` divider tap; write the
wildcard yourself if you really mean the family.

On choosing class_width_mm: freerouting applies the class width to the WHOLE
net, so a width that cannot enter the smallest pad on that net (a USB-C
receptacle's 0.3 mm pads, say) does not fail — it leaves those connections
unrouted and the congestion shows up somewhere else entirely. Size it against
the narrowest pad the net has to reach, not just against the current.

Run immediately after create_pcb; the .kicad_pro is regenerated there.

Idempotent: every pattern already assigned to the power class is stripped
before the resolved set is written back sorted, so a re-run is byte-identical
and a net dropped from netlist.csv does not leave a rule behind. That strip
also clears the ineffective generated patterns, which is the point.

Config consumed: [nets.power] (patterns, class_width_mm, and optionally
class). The pass reads the .kicad_pro beside --board.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Running the script by path already puts this directory on sys.path; the
# insert only covers the isolated-mode / -P invocations where it does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import emit, fail, load_config, pass_parser  # noqa: E402


def log(msg: str) -> None:
    """Progress goes to stderr. Exactly one JSON line goes to stdout."""
    print(msg, file=sys.stderr)


def net_matches(net: str, pattern: str) -> bool:
    """Anchored, case-insensitive glob — `*` any run, `?` one character.

    MIRRORS scripts/validate_gerbers.py:net_matches. The same [nets.power]
    patterns decide what this pass pins and what the gerber checker holds to
    min_width_mm; if the two disagree about what a pattern means, a board can
    pass the checker on nets that were never pinned. Keep them identical.
    """
    regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, net, re.I) is not None


def netlist_nets(path: Path) -> list[str]:
    """Distinct real net names from netlist.csv, in file order."""
    if not path.exists():
        fail(f"{path}: netlist not found")
    seen: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Net,"):
            continue
        f = line.split(",")
        if len(f) < 3 or not f[0]:
            fail(f"{path}:{lineno}: expected at least Net,RefDes,Pin — got {line!r}")
        net = f[0].strip()
        if net != "NC" and net not in seen:
            seen.append(net)
    if not seen:
        fail(f"{path}: no nets")
    return seen


def main() -> int:
    ap = pass_parser("apply_netclasses", board=True, netlist=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        fail(f"{args.config}: config not found or empty")

    power = (cfg.get("nets") or {}).get("power") or {}
    patterns = list(power.get("patterns") or [])
    if not patterns:
        fail("board.toml needs [nets.power] patterns — the power nets to pin by name")
    width = power.get("class_width_mm")
    if width is None:
        fail(
            "board.toml needs [nets.power] class_width_mm — the netclass track width. "
            "Size it against the narrowest pad the net must reach, not only its current."
        )
    class_name = str(power.get("class", "Power"))

    pro = Path(args.board).with_suffix(".kicad_pro")
    if not pro.exists():
        # kct create-pcb writes only the board; bootstrap a minimal project
        # file. It must exist: the .kicad_pro is where net classes live in
        # KiCad 6+, and pcbnew (and therefore the freerouting DSN export)
        # resolves widths from it when it sits beside the board.
        pro.write_text(json.dumps({
            "meta": {"filename": pro.name, "version": 3},
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.25,
                    # 0.2, matching KiCad's DRC default: a smaller netclass
                    # clearance reaches the DSN, freerouting routes to it,
                    # and every squeeze becomes a DRC clearance violation.
                    "clearance": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                }],
                "meta": {"version": 4},
            },
        }, indent=2), encoding="utf-8")
        print(f"  bootstrapped minimal {pro.name}", file=sys.stderr)

    nets = netlist_nets(Path(args.netlist))

    # Resolve every declared pattern to real net names.  A pattern that
    # resolves to nothing is the defect this pass exists to catch.
    resolved: set[str] = set()
    dead: list[str] = []
    for pattern in patterns:
        hits = [n for n in nets if net_matches(n, pattern)]
        if not hits:
            dead.append(pattern)
        resolved.update(hits)
    if dead:
        fail(
            f"{args.config}: [nets.power] pattern(s) matched no net in {args.netlist}: "
            + ", ".join(repr(p) for p in dead)
            + ". A power rule that applies to nothing is how power routes at the default "
            "width with no error anywhere — fix the names or drop the pattern."
        )

    doc = json.loads(pro.read_text(encoding="utf-8"))
    ns = doc.setdefault("net_settings", {})

    classes = ns.setdefault("classes", [])
    cls = next((c for c in classes if c.get("name") == class_name), None)
    if cls is None:
        # kct create-pcb does not write netclasses at all — create the
        # power class here rather than assuming an upstream tool did.
        cls = {
            "name": class_name,
            "clearance": 0.2,
            "via_diameter": 0.6,
            "via_drill": 0.3,
        }
        classes.append(cls)
        print(f"  created {class_name!r} netclass in {pro.name}", file=sys.stderr)
    old_width = cls.get("track_width")
    cls["track_width"] = float(width)

    pats = ns.setdefault("netclass_patterns", [])
    before = len(pats)
    kept = [p for p in pats if p.get("netclass") != class_name]
    stripped = before - len(kept)
    pinned = sorted(resolved)
    ns["netclass_patterns"] = kept + [
        {"netclass": class_name, "pattern": net} for net in pinned
    ]

    pro.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    log(f"  {class_name}.track_width {old_width} -> {width} mm")
    log(f"  stripped {stripped} old {class_name} pattern(s)")
    log(f"  pinned to {class_name}: {', '.join(pinned)}")

    emit(
        "apply_netclasses",
        project=str(pro),
        netclass=class_name,
        track_width_mm=float(width),
        previous_track_width_mm=old_width,
        patterns_declared=patterns,
        nets_pinned=pinned,
        patterns_stripped=stripped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
