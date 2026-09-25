#!/usr/bin/env python3
"""Run the placement loop and keep the best-scoring board.

N rounds of::

    kct placement fix       --strategy anchor --anchor <anchors>
    kct optimize-placement  --seed current
    repair_pads.py          MUST run after every optimiser call
    tuck_in.py

scored after each round with `kct placement check`, and the board with the
FEWEST conflicts is the one kept.  Everything else in the build is
deterministic; the CMA-ES placement search is not, which is why this snapshots
the best-scoring board rather than trusting the last one.  **The last round
isn't the best round** — round 4 regularly scores worse than round 2, and
without the snapshot the whole loop is a coin flip you pay 12 minutes for.

`repair_pads` runs inside the loop, not after it: `kct optimize-placement`
writes absolute board coordinates into footprint-LOCAL pad `(at ...)` nodes,
collapsing both pads of a passive onto one point, and `kct placement check`
would otherwise score garbage geometry.  See repair_pads.py.

`fix_pad_angles` runs once, after the best board is restored: the placement
round-trip drops pad ANGLE terms on rotated footprints, which repair_pads'
regex pass cannot reach.

Anchors
-------
`kct placement fix --strategy anchor` moves ANY part absent from its anchor
list, **locked or not**.  So the list is assembled from all three sources that
can hold a position:

  * every ref placed by `[[floorplan.place]]` in board.toml
  * every mounting hole ref (from `[mounting_holes]`, via
    `add_mounting_holes.hole_refs()` — imported so the two cannot drift)
  * anything extra in `[floorplan] anchors`

plus every `Direct`-tagged part that `direct_connect.py --stage pre` has
already snapped onto an anchored target (`direct_connect.held_refs`,
imported so the two cannot drift). Those are in their final spot before
the loop starts; the optimiser packs around them.

How the sibling passes are run
------------------------------
All three (`repair_pads`, `tuck_in`, `fix_pad_angles`) are run as
**subprocesses** of the sibling scripts in this directory, never imported.
Two reasons: they need different interpreters (`fix_pad_angles` needs
`pcbnew`, which on a host KiCad install imports only under KiCad's bundled
Python; the others need `kicad_tools`), and each is a pass in its own right
with its own contract.  Their one JSON line is captured and folded into this
pass's JSON; their stderr passes through.

Environment
-----------
    KCT           the kicad-tools CLI. Default "kct". Split like a shell
                  word list, so `KCT="uv run --project /path/to/kicad-tools kct"`
                  works.
    KCT_PYTHON    interpreter for repair_pads/tuck_in (needs kicad_tools).
                  Default: this interpreter.
    KICAD_PYTHON  interpreter for fix_pad_angles (needs pcbnew).
                  Default: this interpreter.

Not idempotent in the byte-identical sense, and cannot be: the optimiser is a
stochastic search.  It is *restartable* — it never appends, it always starts
from the board as it stands, it removes any stale best-snapshot from a
previous run before it begins, and it always leaves the best-scoring board of
this run in place.

A `kct` call that fails is a failed ROUND, not a failed build (the optimiser
genuinely times out on a full board): it is reported on stderr and the round
still gets repaired, tucked and scored.  A sibling pass that fails IS a failed
build — those are ours, and a crash means the board may be half-written.

board.toml schema
-----------------
::

    [floorplan]
    anchors = ["U7"]        # optional extras; see Anchors above

    [[floorplan.place]]     # consumed for its `ref` keys only (floorplan.py
    ref = "U1"              # owns the rest)

    [mounting_holes]        # consumed for its ref count/prefix only
    positions = [[5.0, 12.0], [50.0, 12.0]]
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import _lib
from add_mounting_holes import hole_refs
from direct_connect import held_refs

NAME = "place"
HERE = Path(__file__).resolve().parent

# The optimiser budget, matching the reference build. Deliberately module
# constants and not board.toml keys: they tune a search, they do not describe
# the board.
FIX_TIMEOUT_S = 120
OPTIMIZE_TIME_BUDGET_S = 180
ANCHOR_WEIGHT = 3.0

CONFLICTS_RE = re.compile(r"^Total:\s+(\d+)\s+conflicts", re.M)
UNSCORED = 9999  # a round whose score could not be read never becomes the best


def kct_argv() -> list[str]:
    return shlex.split(os.environ.get("KCT", "kct"))


def anchors_for(cfg: dict) -> list[str]:
    """Every ref the placement fix pass must hold still, deduped, in order."""
    out: list[str] = []
    seen: set[str] = set()
    fp = cfg.get("floorplan") or {}
    for entry in fp.get("place") or []:
        ref = (entry or {}).get("ref")
        if isinstance(ref, str) and ref.strip() and ref not in seen:
            seen.add(ref.strip())
            out.append(ref.strip())
    for ref in hole_refs(cfg):
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    for ref in fp.get("anchors") or []:
        ref = str(ref).strip()
        if ref and ref not in seen:
            seen.add(ref)
            out.append(ref)
    if not out:
        _lib.fail(
            "no anchors: board.toml has no [[floorplan.place]] entries, no "
            "[mounting_holes] positions and no [floorplan] anchors. "
            "`kct placement fix --strategy anchor` would move every part on "
            "the board, including the hand-placed ones."
        )
    return out


def run_kct(args: list[str], *, label: str) -> bool:
    """Run a kct subcommand. Returns False (and reports) on a non-zero exit."""
    cmd = kct_argv() + args
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"  WARNING {label} failed (exit {p.returncode})", file=sys.stderr)
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"    {line}", file=sys.stderr)
        return False
    return True


def conflict_count(board: Path) -> int | None:
    """Conflicts reported by `kct placement check`, or None if unreadable."""
    p = subprocess.run(
        kct_argv() + ["placement", "check", str(board)],
        capture_output=True,
        text=True,
    )
    last = None
    for m in CONFLICTS_RE.finditer(p.stdout):
        last = m  # the summary is the last "Total: N conflicts" line
    return int(last.group(1)) if last else None


def run_pass(script: str, board: Path, args, *, python: str) -> dict:
    """Run a sibling pass and return its JSON line. Non-zero exit is fatal."""
    cmd = [
        python,
        str(HERE / script),
        "--board",
        str(board),
        "--netlist",
        str(args.netlist),
        "--config",
        str(args.config),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stderr:
        sys.stderr.write(p.stderr)
    if p.returncode != 0:
        _lib.fail(f"{script} failed (exit {p.returncode}) — board may be half-written")
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    _lib.fail(f"{script} exited 0 but printed no JSON line — cannot verify what it changed")


def main() -> int:
    ap = _lib.pass_parser(NAME)
    ap.add_argument("--rounds", type=int, default=4, help="placement rounds (default 4)")
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        _lib.fail(f"{board} not found — run create-pcb, floorplan and add_mounting_holes first")
    if args.rounds < 1:
        _lib.fail("--rounds must be at least 1")

    cfg = _lib.load_config(args.config)
    anchors = anchors_for(cfg)
    anchors += [r for r in held_refs(Path(args.netlist), cfg, anchors) if r not in anchors]
    anchor_arg = ",".join(anchors)

    kct_python = os.environ.get("KCT_PYTHON", sys.executable)
    kicad_python = os.environ.get("KICAD_PYTHON", sys.executable)

    snapshot = board.with_name(f"{board.stem}.best{board.suffix}")
    if snapshot.exists():  # stale snapshot from an interrupted run
        snapshot.unlink()

    start = conflict_count(board)
    if start is None:
        _lib.fail(
            "could not read a conflict count from `kct placement check` — "
            "the loop cannot keep the best board if it cannot score one. "
            "Check that kct is on PATH (or set $KCT) and still prints "
            "'Total: N conflicts'."
        )
    best = start
    best_round = 0
    shutil.copy2(board, snapshot)
    print(f"  start: {best} conflicts  ({len(anchors)} anchors)", file=sys.stderr)

    rounds: list[dict] = []
    completed = False
    try:
        for i in range(1, args.rounds + 1):
            ok_fix = run_kct(
                [
                    "placement", "fix", str(board), "-o", str(board),
                    "--strategy", "anchor", "--anchor", anchor_arg,
                    "--timeout", str(FIX_TIMEOUT_S), "-q",
                ],
                label=f"round {i}: kct placement fix",
            )
            _lib.assert_net_table(board)
            ok_opt = run_kct(
                [
                    "optimize-placement", str(board), "-o", str(board),
                    "--seed", "current",
                    "--anchor-weight", str(ANCHOR_WEIGHT),
                    "--time-budget", str(OPTIMIZE_TIME_BUDGET_S), "-q",
                ],
                label=f"round {i}: kct optimize-placement",
            )
            _lib.assert_net_table(board)

            # MUST run after optimize-placement: it writes absolute board
            # coordinates into footprint-LOCAL pad (at ...) nodes. See
            # repair_pads.py.
            rp = run_pass("repair_pads.py", board, args, python=kct_python)
            _lib.assert_net_table(board)
            ti = run_pass("tuck_in.py", board, args, python=kct_python)
            _lib.assert_net_table(board)

            n = conflict_count(board)
            scored = n is not None
            n = n if scored else UNSCORED
            if n < best:
                best = n
                best_round = i
                shutil.copy2(board, snapshot)
                print(f"  round {i}: {n} conflicts  (new best)", file=sys.stderr)
            else:
                note = "" if scored else "  (unscored)"
                print(f"  round {i}: {n} conflicts{note}", file=sys.stderr)

            rounds.append(
                {
                    "round": i,
                    "conflicts": n if scored else None,
                    "kct_fix_ok": ok_fix,
                    "kct_optimize_ok": ok_opt,
                    "pads_repaired": rp.get("repaired"),
                    "tucked": ti.get("moved"),
                }
            )

        completed = True
    finally:
        if completed:
            shutil.copy2(snapshot, board)
            snapshot.unlink()
        elif snapshot.exists():
            # Something failed mid-loop. Keep the best board found so far
            # rather than deleting it along with the run.
            print(f"  best-scoring board so far kept at {snapshot}", file=sys.stderr)

    nets = _lib.assert_net_table(board)
    where = f"round {best_round}" if best_round else "the starting board — no round improved on it"
    print(f"  kept best: {best} conflicts ({where})", file=sys.stderr)

    # The placement round-trip drops pad ANGLE terms on rotated footprints
    # (0.6x1.3 pads coming out axis-aligned and overlapping at 1.1 mm pitch).
    # Enforce pad.orientation = footprint.rotation + library-local angle.
    fpa = run_pass("fix_pad_angles.py", board, args, python=kicad_python)
    nets = _lib.assert_net_table(board)

    _lib.emit(
        NAME,
        board=str(board),
        rounds=args.rounds,
        anchors=len(anchors),
        start_conflicts=start,
        best_conflicts=best,
        best_round=best_round,
        per_round=rounds,
        pad_angles_fixed=fpa.get("pads_fixed"),
        nets=nets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
