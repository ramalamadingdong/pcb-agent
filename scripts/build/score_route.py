#!/usr/bin/env python3
"""Score a routed board, so a placement change is judged by measurement.

/improve-placement proposes floorplan edits after looking at a picture. The
picture proposes; this decides. A trial is kept only if its score beats the
baseline's, compared lexicographically, lower is better:

    1. placement gate    0 if check_placement passes, else 1. A board a person
                         can't plug a cable into loses to anything that routes
                         worse. This is what stops a rotation from "fixing"
                         routing by turning a connector to face the board
    2. DRC gate          0 if drc_sample passed (from the route report), else 1
    3. router left       pad links still missing after Freerouting, before
                         the completion passes: the router's own difficulty
    4. unfinished        pad links still missing on the FINAL board, counted
                         by pcbnew (not finish_routes' own `unfixed` list)
    5. vias
    6. track length (mm, 0.1 mm resolution)

Every number is measured here or by the gates, never read from the router's
log. Inputs are the ROUTED board and the JSON route.py wrote with --report.
Without a report, the DRC is sampled here and "router left" is unknown
(scored as the board's current unrouted count, which after the completion
passes is optimistic, and the JSON says so).

``--baseline score.json`` compares against a previous score and prints
better / worse / same. ``--out`` saves this score for the next comparison.

--netlist is forwarded to check_placement.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

PASS = "score_route"
HERE = Path(__file__).resolve().parent
KEYS = ("placement_fail", "drc_fail", "router_left", "unfinished", "vias", "track_length_mm")


def log(*a) -> None:
    print(*a, file=sys.stderr)


def placement(board: Path, config: str, netlist: str) -> tuple[bool, list[str]]:
    proc = subprocess.run(
        [sys.executable, str(HERE / "check_placement.py"), "--board", str(board),
         "--config", config, "--netlist", netlist],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not data:
        _lib.fail(f"check_placement produced no JSON (exit {proc.returncode}): "
                  f"{proc.stderr[-400:]}")
    fails = [r["check"] + ": " + r["detail"] for r in data.get("rows", [])
             if r.get("status") == "fail"]
    return not fails, fails


def main() -> int:
    ap = _lib.pass_parser(PASS)
    ap.add_argument("--route-report", default=None, help="route.py --report output")
    ap.add_argument("--baseline", default=None, help="a previous score_route --out file")
    ap.add_argument("--out", default=None, help="write this score here")
    args = ap.parse_args()

    import pcbnew

    board = Path(args.board)
    b = pcbnew.LoadBoard(str(board))
    if b is None:
        _lib.fail(f"pcbnew could not load {board} (KiCad major mismatch?)")
    cfg = _lib.load_config(args.config)

    place_ok, place_fails = placement(board, args.config, args.netlist)
    notes = []
    if args.route_report and not Path(args.route_report).exists():
        # the Makefile always passes it; before a route there is none
        print(f"  no route report at {args.route_report}: routing "
              "metrics unavailable", file=sys.stderr)
        args.route_report = None
    if args.route_report:
        rep = json.loads(Path(args.route_report).read_text(encoding="utf-8"))
        drc_ok = bool((rep.get("drc") or {}).get("ok"))
        router_left = (rep.get("unrouted_after_router") or {}).get("pad_links_missing")
        # measured on the final board, not finish_routes' own `unfixed`: that
        # list once named 14 "unfinished" nets on a fully connected board
        unfinished = _lib.unrouted_nets(board)["pad_links_missing"]
        if router_left is None:
            notes.append("route report predates unrouted_after_router")
    else:
        import drc_sample
        _report, drc_ok = drc_sample.sample(board, cfg, int((cfg.get("drc") or {}).get(
            "runs", drc_sample.DEFAULT_RUNS)), fill="copy", netlist=args.netlist)
        router_left = None
        unfinished = _lib.unrouted_nets(board)["pad_links_missing"]
        notes.append("no --route-report: router_left unknown, DRC sampled here")

    stats = _lib.track_stats(b)
    score = {
        "placement_fail": 0 if place_ok else 1,
        "drc_fail": 0 if drc_ok else 1,
        "router_left": router_left if router_left is not None else unfinished,
        "unfinished": unfinished,
        "vias": stats["vias"],
        "track_length_mm": round(stats["track_length_mm"], 1),
    }
    vec = [score[k] for k in KEYS]
    log(f"score {board.name}: " + ", ".join(f"{k}={score[k]}" for k in KEYS))
    for f in place_fails:
        log(f"  placement FAIL {f}")

    verdict = None
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        bvec = [base["score"][k] for k in KEYS]
        verdict = "better" if vec < bvec else ("worse" if vec > bvec else "same")
        first = next((k for k, x, y in zip(KEYS, vec, bvec) if x != y), None)
        log(f"vs baseline {Path(args.baseline).name}: {verdict}"
            + (f" (decided by {first}: {base['score'][first]} -> {score[first]})" if first else ""))

    result = {"board": str(board), "score": score, "vector": vec,
              "placement_failures": place_fails, "off_angle_segments":
              len(stats["off_angle_segments"]), "notes": notes, "verdict": verdict}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"pass": PASS, **result}, indent=1),
                                  encoding="utf-8")
    _lib.emit(PASS, **result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
