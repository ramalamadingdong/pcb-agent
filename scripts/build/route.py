#!/usr/bin/env python3
"""Route the board, off a snapshot, and verify the router actually routed.

Routing is the only slow, non-repeatable step, and importing the router's
output rewrites the board into a format the rest of the tools cannot read.
So it lives here, starting from a snapshot, and placement can be re-run
twenty times without paying for a re-route.

    snapshot -> export DSN -> freerouting -> import SES
             -> post_route_fix -> finish_routes -> cleanup -> fill -> DRC x N

Every stage is one of the passes in this directory, run as a subprocess; each
one's JSON line is collected into this pass's own JSON line under "stages".

THE THINGS THAT ARE NOT PREFERENCES
-----------------------------------
* **Use `-mt 1`.** Freerouting itself warns that multi-threaded optimization
  is broken and generates clearance violations, and it does: `-mt 4` gave
  19-20 DRC errors (5 clearance, 5 shorts, 1 tracks-crossing, 4 mask
  bridges) where `-mt 1` gave ZERO routing violations on the same input, and
  converged FASTER (61 s vs 210 s) because it stops when it stops improving
  instead of grinding out every pass.
* **Run it in the foreground.** Backgrounding it (`nohup ... &`) makes it
  open the .dsn and exit without routing.
* **Each release needs a different Java, so resolve it before the slow
  step.** Running a jar on too old a JRE dies with UnsupportedClassVersionError
  *after* the DSN export has already spent a minute, so the required
  class-file version is read out of the jar itself rather than assumed.
  Override the interpreter with FREEROUTING_JAVA.
* **Count copper segments across the router call.** If it lists nets
  correctly and emits zero copper, that is a silent no-op and its own report
  will not tell you. This pass fails loudly on it.
* **DRC is not deterministic — sample it.** Five runs by default, comparing
  which violation KINDS appear rather than the count.
* `ImportSpecctraSES` + `SaveBoard` rewrites the board into the name-only net
  dialect: the top-level `(net <id> "<name>")` table goes to zero entries.
  KiCad, DRC and every fab export read this fine, but tools with their own
  board parser cannot — so route AFTER the build passes, never before, and
  do not assert the net table downstream of the import. It is asserted once,
  inside export_dsn, which is where an empty one would actually hurt: the
  router would report "nets to route: 0" and do nothing.

Environment: FREEROUTING_JAR (required), FREEROUTING_JAVA (default `java`),
KICAD_CLI (default `kicad-cli`).

board.toml keys consumed: none directly — `--config` is forwarded verbatim to
every pass it runs.

Not ported from the source shell script:
  * the multi-candidate JRE search (`~/.local/java/jdk-*`, a distribution's
    install path). Which JREs a host has is host-specific; the class-file
    check and the FREEROUTING_JAVA override are what survive.
  * the router A/B comparison log (version-vs-version violation counts, the
    confounds tested and ruled out) and the "why not this other router"
    argument. Both are findings about one board's copy of two jars.
  * the download-it-yourself hint on a missing jar — `make setup` and
    `scripts/fetch-freerouting.sh` fetch the pinned, checksum-verified jar.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import count_segments, emit, fail, pass_parser  # noqa: E402

HERE = Path(__file__).resolve().parent

# Unique per-run temp names: an orphaned router process from a killed run once
# overwrote the .ses mid-route and its STALE PLACEMENT was imported,
# scrambling the entire floorplan. Never share these files.
RUN_ID = os.getpid()

# The lines worth putting in front of a human while the router runs.
FR_INTERESTING = re.compile(
    r"Auto-routing|optimization|Saving|ERROR|pass|incomplete|items|Starting|completed",
    re.I)


def log(*a) -> None:
    print(*a, file=sys.stderr)


# ---- freerouting runtime resolution -----------------------------------------

def jar_needs_java(jar: Path) -> int | None:
    """The Java feature version this jar was compiled for, read out of it."""
    try:
        with zipfile.ZipFile(jar) as z:
            mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
            m = re.search(r"^Main-Class:\s*(\S+)", mf, re.M)
            if not m:
                return None
            data = z.read(m.group(1).replace(".", "/") + ".class")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    if len(data) < 8:
        return None
    # bytes 7-8 of a .class file are the big-endian major version; major-44 = Java N
    return int.from_bytes(data[6:8], "big") - 44


def java_feature(java: str) -> int | None:
    try:
        p = subprocess.run([java, "-version"], capture_output=True, text=True)
    except OSError:
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', (p.stderr or "") + (p.stdout or ""))
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    return minor if major == 1 else major     # 1.8 -> 8, 17 -> 17


def jar_major(jar: Path) -> int:
    m = re.match(r"freerouting-(\d+)\.", jar.name)
    return int(m.group(1)) if m else 1


# ---- pass plumbing -----------------------------------------------------------

def run_pass(script: str, *argv: str, allow: tuple[int, ...] = (0,)) -> tuple[dict, int]:
    """Run a build pass, return (its JSON line, exit code).

    stdout is captured because the JSON line is the pass's report; stderr is
    inherited so its progress lands in front of the human as it happens.
    """
    cmd = [sys.executable, str(HERE / script), *argv]
    log("+", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    data: dict = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if proc.returncode not in allow:
        fail(f"{script}: exit {proc.returncode} — routing stopped, "
             f"board left as {script} found it")
    return data, proc.returncode


def run_drc(kicad_cli: str, board: Path, out: Path) -> dict:
    proc = subprocess.run(
        [kicad_cli, "pcb", "drc", "--format", "json", "--severity-error",
         "-o", str(out), str(board)],
        capture_output=True, text=True)
    if not out.exists():
        fail(f"kicad-cli pcb drc wrote no report ({proc.returncode}): "
             f"{(proc.stderr or proc.stdout)[:400]}")
    report = json.loads(out.read_text(encoding="utf-8"))
    kinds = Counter(v.get("type", "?") for v in report.get("violations", []))
    return {
        "kinds": dict(kinds),
        "violations": sum(kinds.values()),
        "unconnected": len(report.get("unconnected_items", [])),
    }


def fill_zones(board: Path) -> None:
    """Final authoritative fill before DRC.

    Every pass upstream fills as it saves; this is here because DRC refills
    zones with its own filler when it finds them stale, and that invents
    thermal-relief errors that are not on the board.
    """
    import pcbnew  # local: the freerouting/kicad-cli preflight reports first
    b = pcbnew.LoadBoard(str(board))
    if b is None:
        fail(f"pcbnew could not load {board} (KiCad major mismatch?)")
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(str(board), b)


def main() -> int:
    ap = pass_parser("route")
    ap.add_argument("--snapshot", default="pre_route.kicad_pcb",
                    help="post-build snapshot to route from (never modified)")
    ap.add_argument("--passes", type=int, default=100,
                    help="freerouting max passes (-mp)")
    ap.add_argument("--drc-runs", type=int, default=5,
                    help="DRC samples; DRC is not deterministic (min 1)")
    ap.add_argument("--keep-intermediates", action="store_true",
                    help="keep the .dsn/.ses instead of deleting them")
    args = ap.parse_args()

    board = Path(args.board)
    snapshot = Path(args.snapshot)
    workdir = board.parent if str(board.parent) else Path(".")

    if not snapshot.exists():
        fail(f"{snapshot}: no snapshot to route from — run the build first")
    if snapshot.resolve() == board.resolve():
        fail("--board and --snapshot are the same file; routing would "
             "destroy the snapshot this pass restarts from")

    # ---- preflight: everything that can fail before the slow step -----------
    jar_env = os.environ.get("FREEROUTING_JAR")
    if not jar_env:
        fail("FREEROUTING_JAR is not set (see `make setup` / "
             "scripts/fetch-freerouting.sh)")
    jar = Path(jar_env)
    if not jar.is_file():
        fail(f"{jar}: no freerouting jar there (FREEROUTING_JAR)")

    java = os.environ.get("FREEROUTING_JAVA") or "java"
    have = java_feature(java)
    if have is None:
        fail(f"{java}: not runnable / unreadable version — set FREEROUTING_JAVA")
    need = jar_needs_java(jar)
    if need is not None and have < need:
        fail(f"{jar.name} needs Java {need}; {java} is {have}. Point "
             f"FREEROUTING_JAVA at a newer JRE (leave JAVA_HOME alone — other "
             f"toolchains on this machine may need the older one).")
    log(f"freerouting: {jar.name}  on Java {have}"
        + (f" (needs {need})" if need else ""))

    kicad_cli = os.environ.get("KICAD_CLI", "kicad-cli")
    if shutil.which(kicad_cli) is None:
        fail(f"{kicad_cli}: not on PATH (set KICAD_CLI) — the DRC sampling at "
             f"the end of this pass cannot run")

    dsn = workdir / f"board.{RUN_ID}.dsn"
    ses = workdir / f"board.{RUN_ID}.ses"
    fr_log = workdir / "freerouting.log"

    # ---- 0. restart from the snapshot ---------------------------------------
    # Every re-run starts from the same bytes, so this pass is idempotent even
    # though the router itself is not repeatable.
    shutil.copyfile(snapshot, board)
    seg_before = count_segments(board)
    log(f"=== 0/7  snapshot {snapshot} -> {board}  ({seg_before} segments)")

    stages: dict[str, dict] = {}

    # ---- 1. export DSN -------------------------------------------------------
    log("=== 1/7  export DSN ==========================================")
    stages["export_dsn"], _ = run_pass(
        "export_dsn.py", "--board", str(board), "--config", args.config,
        "--netlist", args.netlist, "--out", str(dsn))

    # ---- 2. freerouting ------------------------------------------------------
    log("=== 2/7  freerouting (single-threaded, foreground) ============")
    # -mt 1 is not a performance choice, it is a correctness one.
    fr_args = [java, "-jar", str(jar), "-de", str(dsn), "-do", str(ses),
               "-mp", str(args.passes), "-mt", "1"]
    if jar_major(jar) >= 2:
        # 2.x opens a GUI unless told not to (which hangs a headless run) and
        # uploads anonymous analytics by default, hence -da.
        # --user_data_path keeps freerouting.json/log out of $HOME.
        fr_args += ["--gui.enabled=false", "-da",
                    f"--user_data_path={workdir / '.freerouting'}"]
    elif not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        # 1.9.0's main() calls Toolkit.getScreenSize() even in -de/-do batch
        # mode and dies with HeadlessException without a display. xvfb-run
        # gives it a virtual framebuffer — nothing renders anywhere; measured
        # to route a real 4-layer DSN to completion. The container ships
        # xvfb for exactly this.
        fr_args = ["xvfb-run", "-a"] + fr_args
    log("+", " ".join(fr_args))
    proc = subprocess.run(fr_args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    fr_log.write_text(proc.stdout or "", encoding="utf-8")
    for line in (proc.stdout or "").splitlines():
        if FR_INTERESTING.search(line):
            log("   ", line.rstrip())
    if proc.returncode != 0:
        fail(f"freerouting exited {proc.returncode} — see {fr_log}")
    if not ses.exists() or ses.stat().st_size == 0:
        fail(f"freerouting wrote no session file at {ses} — see {fr_log}")

    # ---- 3. import SES -------------------------------------------------------
    log("=== 3/7  import SES ==========================================")
    stages["import_ses"], _ = run_pass(
        "import_ses.py", "--board", str(board), "--config", args.config,
        "--netlist", args.netlist, "--ses", str(ses), "--out", str(board))

    # ---- VERIFY RATHER THAN TRUST -------------------------------------------
    # A router that lists the nets correctly and emits zero copper reports
    # success. Segment count across the router+import is the independent
    # check that it did anything at all.
    seg_routed = count_segments(board)
    log(f"segments: {seg_before} before -> {seg_routed} after router+import")
    if seg_routed <= seg_before:
        fail(f"SILENT NO-OP: the router reported success and the board gained "
             f"no copper ({seg_before} segments before, {seg_routed} after). "
             f"Check {fr_log} for 'nets to route: 0' — an empty top-level net "
             f"table, or a DSN whose layers are all (type power), produces "
             f"exactly this.")

    # ---- 4-6. the completion stack ------------------------------------------
    log("=== 4/7  post_route_fix ======================================")
    stages["post_route_fix"], _ = run_pass(
        "post_route_fix.py", "--board", str(board), "--config", args.config,
        "--netlist", args.netlist)

    log("=== 5/7  finish_routes =======================================")
    # Exit 1 means "nets remain unfinished", which is a reported result on a
    # board that spends layers on planes — not a reason to abandon the route.
    # Any other non-zero code is a real failure and stops the pass.
    fin, rc = run_pass("finish_routes.py", "--board", str(board),
                       "--config", args.config, "--netlist", args.netlist,
                       allow=(0, 1))
    stages["finish_routes"] = fin
    if rc == 1:
        log(f"finish_routes: {len(fin.get('unfixed', []))} net(s) unfinished: "
            f"{fin.get('unfixed')}")

    log("=== 6/7  cleanup + fill ======================================")
    stages["cleanup_pass"], _ = run_pass(
        "cleanup_pass.py", "--board", str(board), "--config", args.config,
        "--netlist", args.netlist)
    fill_zones(board)

    # ---- 7. DRC, sampled ----------------------------------------------------
    log("=== 7/7  DRC x%d =============================================="
        % max(1, args.drc_runs))
    runs = []
    for i in range(max(1, args.drc_runs)):
        out = workdir / f"drc_routed.{RUN_ID}.{i}.json"
        r = run_drc(kicad_cli, board, out)
        out.unlink(missing_ok=True)
        runs.append(r)
        log(f"    run {i + 1}: {r['violations']} violation(s) in "
            f"{len(r['kinds'])} kind(s), {r['unconnected']} unconnected")
    kind_sets = [set(r["kinds"]) for r in runs]
    every = set.intersection(*kind_sets) if kind_sets else set()
    any_ = set.union(*kind_sets) if kind_sets else set()
    unstable = sorted(any_ - every)
    if unstable:
        # DRC is nondeterministic: a kind that shows up in some runs and not
        # others is real, and counting one run would have missed it.
        log(f"DRC kinds seen in some runs but not all: {unstable}")
    drc = {
        "runs": len(runs),
        "kinds_every_run": sorted(every),
        "kinds_unstable": unstable,
        "counts_per_run": [r["violations"] for r in runs],
        "unconnected_per_run": [r["unconnected"] for r in runs],
    }

    if not args.keep_intermediates:
        dsn.unlink(missing_ok=True)
        ses.unlink(missing_ok=True)

    emit(
        "route",
        board=str(board),
        snapshot=str(snapshot),
        passes=args.passes,
        jar=jar.name,
        java=have,
        segments_before=seg_before,
        segments_after_router=seg_routed,
        segments_final=count_segments(board),
        unfixed=stages.get("finish_routes", {}).get("unfixed", []),
        drc=drc,
        stages=stages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
