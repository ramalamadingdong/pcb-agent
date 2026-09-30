#!/usr/bin/env python3
"""Level 1: ERC, run by the pipeline instead of by hand, and gated.

Before this pass, ERC existed only as a committed `erc_report.json` someone
had produced by hand, and nothing re-ran it after a netlist edit. It runs as
part of `make check` now:

    kicad-cli sch erc --format json --severity-all

and exits non-zero on any error-severity violation (or GUI exclusion) that is
not in the ledger, and on any ledger entry that matched nothing. Warnings are
always reported and gated only with ``gate_warnings = true``. Same ledger
rules as DRC (drc_sample.py)::

    [erc]
    gate_warnings = false

    [[erc.accept]]
    kind   = "pin_not_driven"
    refs   = ["U3"]        # exact set of refdes the violation names
    count  = 1             # per-report, default 1
    reason = "..."         # required

ERC is deterministic, so one run. It reads the schematic (--schematic),
which generate_schematic.py wrote and verified against netlist.csv.

Environment: KICAD_CLI (default ``kicad-cli``). --netlist is accepted for
contract uniformity and unused.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
from drc_sample import apply_accepts, kicad_cli, load_accepts, normalise  # noqa: E402

PASS = "erc_check"


def log(*a) -> None:
    print(*a, file=sys.stderr)


def main() -> int:
    ap = _lib.pass_parser(PASS, board=False, schematic=True)
    args = ap.parse_args()
    sch = Path(args.schematic)
    if not sch.exists():
        _lib.fail(f"{sch}: no schematic -- run the build first")
    cfg = _lib.load_config(args.config)
    accepts = load_accepts(cfg, "erc")
    gate_warnings = bool((cfg.get("erc") or {}).get("gate_warnings", False))

    with tempfile.TemporaryDirectory(prefix="erc_check.") as td:
        out = Path(td) / "erc.json"
        proc = subprocess.run(
            [*kicad_cli(), "sch", "erc", "--format", "json", "--severity-all",
             "-o", str(out), str(sch)],
            capture_output=True, text=True)
        if not out.exists():
            _lib.fail(f"kicad-cli sch erc wrote no report ({proc.returncode}): "
                      f"{(proc.stderr or proc.stdout)[:400]}")
        rep = json.loads(out.read_text(encoding="utf-8"))

    found = [normalise(v, "erc") for s in rep.get("sheets", [])
             for v in s.get("violations", [])]
    accepted, unaccepted, used = apply_accepts(found, accepts)

    def gated(v: dict) -> bool:
        return v["severity"] in ("error", "exclusion") or gate_warnings

    failing = [v for v in unaccepted if gated(v)]
    reported = [v for v in unaccepted if not gated(v)]
    stale = [a for a, n in zip(accepts, used) if n == 0]
    sev = Counter(v["severity"] for v in found)
    log(f"=== ERC {sch}: {sev.get('error', 0)} error(s), "
        f"{sev.get('warning', 0)} warning(s), {sev.get('exclusion', 0)} exclusion(s)")

    for a, n in zip(accepts, used):
        if n:
            log(f"  accepted: {a['kind']} {sorted(a['refs'])} x{n} -- {a['reason']}")
    for v in failing:
        log(f"  FAIL  {v['severity']} {v['kind']}: " + " ;; ".join(v["items"]))
    for a in stale:
        log(f"  FAIL  stale [[erc.accept]] {a['kind']} refs={sorted(a['refs'])}: "
            f"matched nothing")
    for kind, n in sorted(Counter(v["kind"] for v in reported).items()):
        refs = sorted({r for v in reported if v["kind"] == kind for r in v["refs"]})
        log(f"  warn  {kind} x{n}" + (f": {', '.join(refs[:12])}" if refs else "")
            + (" ..." if len(refs) > 12 else ""))
    if not failing and not stale:
        log("  PASS  no unaccepted ERC errors")

    ok = not failing and not stale
    _lib.emit(
        PASS,
        schematic=str(sch),
        ok=ok,
        kicad_version=rep.get("kicad_version"),
        errors=sev.get("error", 0),
        warnings=sev.get("warning", 0),
        exclusions=sev.get("exclusion", 0),
        failing=[{"kind": v["kind"], "severity": v["severity"], "items": list(v["items"])}
                 for v in failing],
        warnings_by_kind=dict(Counter(v["kind"] for v in reported)),
        accepted=[{"kind": a["kind"], "refs": sorted(a["refs"]), "matched": n,
                   "reason": a["reason"]} for a, n in zip(accepts, used) if n],
        stale_accepts=[{"kind": a["kind"], "refs": sorted(a["refs"])} for a in stale],
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
