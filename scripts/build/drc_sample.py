#!/usr/bin/env python3
"""Sample DRC N times and GATE on it. A violation nobody explained fails.

Two things this pass exists to stop:

* **Counting one DRC run.** KiCad's DRC is not deterministic, so a single run
  can miss a violation that the next run reports. It runs N times (default
  5), and a violation seen in ANY run counts. The kinds that appear in some
  runs but not all are reported by name, never averaged away.
* **Reporting without failing.** `route.py` used to print the violations and
  exit 0, so a board with unexplained DRC errors still built green. That is
  swapping a measurement for an assumption, which CLAUDE.md forbids. This
  pass exits non-zero on:
    - any error-severity violation not in the ledger (``[[drc.accept]]``),
    - any unconnected item, in any run,
    - any schematic-parity issue about connectivity (``GATED_PARITY``: the
      board's nets disagree with the schematic, which level 0 proved equal
      to netlist.csv). KiCad ships these as warnings, so they are gated by
      kind, not severity,
    - any EXCLUSION set in the KiCad GUI (an exclusion is an acceptance
      stored where no reviewer reads it; move it into the ledger with a
      reason),
    - any ledger entry that matched nothing (a stale acceptance is a check
      that succeeds forever),
    - warnings, only when ``gate_warnings = true``. They are always sampled
      and always reported.

board.toml
----------
::

    [drc]
    runs          = 5        # optional, default 5 (--runs overrides)
    gate_warnings = false    # optional

    [[drc.accept]]
    kind   = "copper_edge_clearance"   # the DRC `type`, required
    refs   = ["J4"]          # EXACT set of refdes named in the violation's
                             # items (may be [] for a via or board text)
    nets   = ["GND"]         # optional: these nets must all appear
    count  = 4               # optional, default 1: how many violations this
                             # entry may absorb PER RUN. A fifth J4 pad
                             # starting to violate is a new finding, not
                             # more of the old one
    reason = "..."           # required. The written reason IS the point

Refs are read from the item descriptions KiCad writes (``Footprint C8``,
``Pad 2 [GND] of J4 on F.Cu``, ``PTH pad 10 [SCL_3V3] of J13``). Nets are
read from the bracketed names in the same text.

Zones
-----
DRC refills stale zones with its own filler, which invents thermal-relief
errors that are not on the board. So zones are filled first with
pcbnew.ZONE_FILLER. ``route.py`` does that on the real board, which it is
rewriting anyway. As a checker (``make check``) this pass writes nothing:
it fills and checks a COPY in a temp directory, together with the
.kicad_pro that holds the rules and the .kicad_sch that parity reads.

Environment: KICAD_CLI (default ``kicad-cli``). --netlist is accepted for
contract uniformity and unused.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

PASS = "drc_sample"
DEFAULT_RUNS = 5

# Parity kinds that mean the board's CONNECTIVITY disagrees with the
# schematic. KiCad 10 ships all three at severity "warning", so gating on
# severity alone would pass a board whose pad nets contradict netlist.csv.
# Other parity kinds follow their severity: `extra_footprint` is every
# mounting hole and fiducial the build adds, and `footprint_symbol_mismatch`
# is a library-nickname difference, not a wiring one.
GATED_PARITY = {"net_conflict", "missing_footprint", "duplicate_footprints"}

REF_RE = re.compile(r"\b(?:Footprint|Symbol|of)\s+([A-Za-z]+[0-9]+[A-Za-z0-9_]*)\b")
NET_RE = re.compile(r"\[([^\]]+)\]")


def log(*a) -> None:
    print(*a, file=sys.stderr)


def kicad_cli() -> list[str]:
    cli = shlex.split(os.environ.get("KICAD_CLI", "kicad-cli"))
    if shutil.which(cli[0]) is None:
        _lib.fail(f"{cli[0]}: not on PATH (set KICAD_CLI)")
    return cli


# ---- reading a report -------------------------------------------------------

def refs_of(items: list[dict]) -> frozenset[str]:
    return frozenset(r for it in items for r in REF_RE.findall(it.get("description", "")))


def nets_of(items: list[dict]) -> frozenset[str]:
    return frozenset(n for it in items for n in NET_RE.findall(it.get("description", "")))


def normalise(v: dict, source: str) -> dict:
    items = v.get("items", [])
    return {
        "kind": v.get("type", "?"),
        "severity": v.get("severity", "?"),
        "source": source,                       # violations / parity
        "refs": refs_of(items),
        "nets": nets_of(items),
        "items": tuple(it.get("description", "") for it in items),
    }


def identity(v: dict) -> tuple:
    """Stable across runs: what the violation is about, not where DRC put its marker."""
    return (v["source"], v["kind"], v["severity"], v["items"])


# ---- the ledger --------------------------------------------------------------

def load_accepts(cfg: dict, table: str) -> list[dict]:
    out = []
    for i, a in enumerate((cfg.get(table) or {}).get("accept") or []):
        where = f"[[{table}.accept]] #{i + 1}"
        if not a.get("kind"):
            _lib.fail(f"{where}: no kind")
        if not str(a.get("reason") or "").strip():
            _lib.fail(f"{where} ({a['kind']}): no reason. An acceptance without "
                      f"a written reason is a waved-off warning")
        out.append({
            "kind": str(a["kind"]),
            "refs": frozenset(str(r) for r in (a.get("refs") or [])),
            "nets": frozenset(str(n) for n in (a.get("nets") or [])),
            "count": int(a.get("count", 1)),
            "reason": str(a["reason"]).strip(),
        })
    return out


def apply_accepts(run: list[dict], accepts: list[dict]) -> tuple[list, list, list[int]]:
    """Split one run into (accepted, unaccepted) and count matches per entry."""
    used = [0] * len(accepts)
    accepted, unaccepted = [], []
    for v in run:
        hit = None
        for i, a in enumerate(accepts):
            if (a["kind"] == v["kind"] and a["refs"] == v["refs"]
                    and a["nets"] <= v["nets"] and used[i] < a["count"]):
                hit = i
                break
        if hit is None:
            unaccepted.append(v)
        else:
            used[hit] += 1
            accepted.append((hit, v))
    return accepted, unaccepted, used


# ---- running DRC -------------------------------------------------------------

def fill_zones(board: Path) -> None:
    """Fill with the real filler and save. Callers own the file they pass in."""
    import pcbnew
    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew could not load {board} (KiCad major mismatch?)")
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(board), b)


def run_drc(cli: list[str], board: Path, out: Path, parity: bool) -> dict:
    cmd = [*cli, "pcb", "drc", "--format", "json", "--severity-all"]
    if parity:
        cmd.append("--schematic-parity")
    proc = subprocess.run([*cmd, "-o", str(out), str(board)],
                          capture_output=True, text=True)
    if not out.exists():
        _lib.fail(f"kicad-cli pcb drc wrote no report ({proc.returncode}): "
                  f"{(proc.stderr or proc.stdout)[:400]}")
    rep = json.loads(out.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)
    found = [normalise(v, "violations") for v in rep.get("violations", [])]
    found += [normalise(v, "parity") for v in rep.get("schematic_parity", [])]
    return {
        "found": found,
        "unconnected": [tuple(i.get("description", "") for i in u.get("items", []))
                        for u in rep.get("unconnected_items", [])],
    }


def stage_copy(board: Path, tmp: Path) -> Path:
    """Copy the board and what DRC reads beside it (rules, parity) into tmp."""
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_sch"):
        src = board.with_suffix(suffix)
        if src.exists():
            shutil.copy2(src, tmp / src.name)
    for name in ("fp-lib-table", "sym-lib-table"):
        if (board.parent / name).exists():
            shutil.copy2(board.parent / name, tmp / name)
    return tmp / board.name


def sample(board: Path, cfg: dict, runs: int, *, fill: str) -> tuple[dict, bool]:
    """Run DRC `runs` times and judge it against the ledger.

    fill: "inplace" fills `board` itself first, "copy" fills and checks a temp
    copy (the board is not written), "none" trusts that the caller filled.
    """
    cli = kicad_cli()
    accepts = load_accepts(cfg, "drc")
    gate_warnings = bool((cfg.get("drc") or {}).get("gate_warnings", False))
    parity = board.with_suffix(".kicad_sch").exists()
    if not parity:
        log(f"no {board.with_suffix('.kicad_sch').name} beside the board: "
            f"schematic parity NOT checked")

    with tempfile.TemporaryDirectory(prefix="drc_sample.") as td:
        target = board
        if fill == "copy":
            target = stage_copy(board, Path(td))
        if fill in ("copy", "inplace"):
            fill_zones(target)
        results = []
        for i in range(max(1, runs)):
            r = run_drc(cli, target, Path(td) / f"drc.{i}.json", parity)
            results.append(r)
            sev = Counter(v["severity"] for v in r["found"])
            log(f"    run {i + 1}: {sev.get('error', 0)} error(s), "
                f"{sev.get('warning', 0)} warning(s), "
                f"{sev.get('exclusion', 0)} exclusion(s), "
                f"{sum(1 for v in r['found'] if v['source'] == 'parity')} parity, "
                f"{len(r['unconnected'])} unconnected")

    # ---- judge every run; anything unaccepted in ANY run is a finding --------
    unaccepted: dict[tuple, dict] = {}
    matched_max = [0] * len(accepts)
    for r in results:
        _acc, bad, used = apply_accepts(r["found"], accepts)
        matched_max = [max(a, b) for a, b in zip(matched_max, used)]
        per_run = Counter(identity(v) for v in bad)
        for v in bad:
            k = identity(v)
            if k not in unaccepted:
                unaccepted[k] = {**v, "runs_seen": 0, "per_run": 0}
        for k, n in per_run.items():
            # DISTINCT runs: one run may report the same thing twice
            unaccepted[k]["runs_seen"] += 1
            unaccepted[k]["per_run"] = max(unaccepted[k]["per_run"], n)

    def gated(v: dict) -> bool:
        if v["source"] == "parity" and v["kind"] in GATED_PARITY:
            return True
        if v["severity"] in ("error", "exclusion"):
            return True
        return gate_warnings

    failing = [v for v in unaccepted.values() if gated(v)]
    reported = [v for v in unaccepted.values() if not gated(v)]
    stale = [a for a, n in zip(accepts, matched_max) if n == 0]
    unconnected = max((len(r["unconnected"]) for r in results), default=0)

    kind_sets = [{v["kind"] for v in r["found"] if v["severity"] != "warning"}
                 for r in results]
    every = set.intersection(*kind_sets) if kind_sets else set()
    any_ = set.union(*kind_sets) if kind_sets else set()
    unstable = sorted(any_ - every)

    # ---- say it --------------------------------------------------------------
    for a, n in zip(accepts, matched_max):
        if n:
            log(f"  accepted: {a['kind']} {sorted(a['refs']) or ''} x{n} -- {a['reason']}")
    for v in sorted(failing, key=lambda v: (v["kind"], v["items"])):
        log(f"  FAIL  {v['severity']} {v['kind']} ({v['runs_seen']}/{len(results)} runs): "
            + " ;; ".join(v["items"]))
    by_kind: dict[tuple, list] = {}
    for v in reported:
        by_kind.setdefault((v["source"], v["kind"]), []).append(v)
    for (src, kind), vs in sorted(by_kind.items()):
        if len(vs) > 5:     # 54 parity lines bury the ones that matter
            refs = sorted({r for v in vs for r in v["refs"]})
            log(f"  warn  {src} {kind} x{len(vs)}: {', '.join(refs[:12])}"
                + (" ..." if len(refs) > 12 else ""))
            continue
        for v in sorted(vs, key=lambda v: v["items"]):
            log(f"  warn  {kind} ({v['runs_seen']}/{len(results)} runs"
                + (f", x{v['per_run']} each" if v["per_run"] > 1 else "") + "): "
                + " ;; ".join(v["items"]))
    for a in stale:
        log(f"  FAIL  stale [[drc.accept]] {a['kind']} refs={sorted(a['refs'])}: "
            f"matched nothing in {len(results)} runs. Remove it, or it hides the "
            f"next real violation of that shape")
    if unconnected:
        log(f"  FAIL  {unconnected} unconnected item(s)")
    if unstable:
        log(f"  kinds seen in some runs but not all: {unstable}")

    ok = not failing and not stale and unconnected == 0

    def row(v: dict) -> dict:
        return {"kind": v["kind"], "severity": v["severity"], "source": v["source"],
                "items": list(v["items"]), "runs_seen": v["runs_seen"],
                "per_run": v["per_run"]}

    report = {
        "runs": len(results),
        "ok": ok,
        "parity_checked": parity,
        "kinds_every_run": sorted(every),
        "kinds_unstable": unstable,
        "errors_per_run": [sum(1 for v in r["found"] if v["severity"] == "error")
                           for r in results],
        "warnings_per_run": [sum(1 for v in r["found"] if v["severity"] == "warning")
                             for r in results],
        "unconnected_per_run": [len(r["unconnected"]) for r in results],
        "failing": [row(v) for v in failing],
        "warnings_unaccepted": [row(v) for v in reported],
        "accepted": [{"kind": a["kind"], "refs": sorted(a["refs"]), "matched": n,
                      "reason": a["reason"]} for a, n in zip(accepts, matched_max) if n],
        "stale_accepts": [{"kind": a["kind"], "refs": sorted(a["refs"])} for a in stale],
    }
    return report, ok


def main() -> int:
    ap = _lib.pass_parser(PASS)
    ap.add_argument("--runs", type=int, default=None,
                    help=f"DRC samples (default [drc] runs, else {DEFAULT_RUNS})")
    ap.add_argument("--fill", choices=("copy", "inplace", "none"), default="copy",
                    help="copy (default): fill and check a temp copy, write nothing")
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board}: no board")
    cfg = _lib.load_config(args.config)
    runs = args.runs or int((cfg.get("drc") or {}).get("runs", DEFAULT_RUNS))

    log(f"=== DRC x{runs} on {board} (fill: {args.fill})")
    report, ok = sample(board, cfg, runs, fill=args.fill)
    _lib.emit(PASS, board=str(board), **report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
