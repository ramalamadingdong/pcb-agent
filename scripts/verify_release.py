#!/usr/bin/env python3
"""verify_release — prove a release directory is still what was ordered.

    python3 scripts/verify_release.py examples/<board>/release/rev-1

Run it before a review, after a review, and before re-ordering. Three
levels; the first needs nothing but Python:

  1. HASHES. Every file matches MANIFEST.sha256, none is missing, and no
     unlisted file has appeared. KiCad's own UI droppings (.kicad_prl,
     fp-info-cache, backups, lock files) are reported and ignored -- opening
     the project makes them, and they change nothing a fab sees. A reviewer
     who pressed save changes a hash, and this fails naming the file.
  2. FAB ZIP. Every member of the ordered zip is byte-identical to the file
     beside it in fab/.
  3. RE-DERIVE (needs pcbnew + kicad-cli, i.e. the container). On a scratch
     copy: run export_fab.py against the released board -- which runs
     check_parity first -- and compare every regenerated file with the
     released one (creation-date lines aside), then validate_gerbers with the
     released netlist export. So: the board a reviewer opens produces, today,
     the package that shipped. A different KiCad version can legitimately
     plot different bytes; the diff then names the files, and
     release.json says which version made the originals.

Exit 0 only if every level that ran passed. Level 3 without KiCad is a
SKIP, and a SKIP is not a PASS: the exit code is 2 in that case.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

SIDECARS = ("*.kicad_prl", "fp-info-cache", "*.lck", "~*", "_autosave-*",
            "#auto_saved_files#", "*-backups/*")

# Lines that differ between two exports of the same board: timestamps only.
VOLATILE = re.compile(r"CreationDate|^G04 Created by .* date|^; DRILL file .* date")

failures = 0
skips = 0


def say(status: str, what: str, detail: str = "") -> None:
    global failures, skips
    failures += status == "FAIL"
    skips += status == "SKIP"
    print(f"{status:4}  {what}" + (f"\n      {detail}" if detail else ""))


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def is_sidecar(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(Path(rel).name, pat)
               for pat in SIDECARS)


def level_hashes(rel_dir: Path) -> None:
    man = rel_dir / "MANIFEST.sha256"
    if not man.exists():
        say("FAIL", "hashes", f"{man} missing — not a release directory")
        return
    listed: dict[str, str] = {}
    for line in man.read_text(encoding="utf-8").splitlines():
        h, _, p = line.partition("  ")
        listed[p] = h
    changed, missing = [], []
    for p, h in listed.items():
        f = rel_dir / p
        if not f.exists():
            missing.append(p)
        elif sha256(f) != h:
            changed.append(p)
    present = {f.relative_to(rel_dir).as_posix() for f in rel_dir.rglob("*") if f.is_file()}
    present -= {"MANIFEST.sha256", "release.json"}
    extra = sorted(p for p in present - set(listed) if not is_sidecar(p))
    ignored = sorted(p for p in present - set(listed) if is_sidecar(p))
    if changed or missing or extra:
        say("FAIL", "hashes", "; ".join(
            x for x in (f"changed: {changed}" if changed else "",
                        f"missing: {missing}" if missing else "",
                        f"unlisted: {extra}" if extra else "") if x))
    else:
        say("PASS", "hashes", f"{len(listed)} files match MANIFEST.sha256"
            + (f" (ignored KiCad sidecars: {ignored})" if ignored else ""))


def level_zip(rel_dir: Path) -> None:
    zips = sorted((rel_dir / "fab").glob("*.zip"))
    if not zips:
        say("SKIP", "fab zip", "no zip in fab/ ([fab] zip = false?)")
        return
    for z in zips:
        bad = []
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                beside = rel_dir / "fab" / name
                if not beside.exists() or zf.read(name) != beside.read_bytes():
                    bad.append(name)
            n = len(zf.namelist())
        if bad:
            say("FAIL", f"fab zip {z.name}", f"members differ from fab/: {bad}")
        else:
            say("PASS", f"fab zip {z.name}", f"{n} members identical to fab/")


def normalised(p: Path) -> list[str]:
    return [ln for ln in p.read_text(errors="replace").splitlines() if not VOLATILE.search(ln)]


def level_rederive(rel_dir: Path) -> None:
    try:
        import pcbnew
    except ImportError:
        say("SKIP", "re-derive", "no pcbnew on this interpreter — run inside the container "
            "(./run.sh python3 scripts/verify_release.py ...)")
        return
    if shutil.which("kicad-cli") is None:
        say("SKIP", "re-derive", "no kicad-cli")
        return
    boards = [p for p in rel_dir.glob("*.kicad_pcb")]
    if len(boards) != 1:
        say("FAIL", "re-derive", f"expected one .kicad_pcb in {rel_dir}, found {len(boards)}")
        return
    if pcbnew.LoadBoard(str(boards[0])) is None:
        say("SKIP", "re-derive", f"pcbnew {pcbnew.Version()} cannot read this board — it was "
            "made by a newer KiCad (release.json names it); run inside the container")
        return
    with tempfile.TemporaryDirectory() as t:
        work = Path(t) / "rel"
        shutil.copytree(rel_dir, work, ignore=shutil.ignore_patterns("fab", *SIDECARS))
        board = work / boards[0].name
        out = work / "fab"
        p = subprocess.run([sys.executable, str(HERE / "build" / "export_fab.py"),
                            "--board", str(board), "--config", str(work / "board.toml"),
                            "--out", str(out)], capture_output=True, text=True)
        if p.returncode != 0:
            say("FAIL", "re-derive: export (parity gate included)", p.stderr.strip()[-1500:])
            return
        say("PASS", "re-derive: parity", "released board is its released schematic")
        if sha256(board) != sha256(boards[0]):
            say("FAIL", "re-derive", "export rewrote the board (aux origin?) — the released "
                "board is not the one the package was plotted from")
        released = {f.relative_to(rel_dir / "fab").as_posix(): f
                    for f in (rel_dir / "fab").rglob("*") if f.is_file() and f.suffix != ".zip"}
        regen = {f.relative_to(out).as_posix(): f
                 for f in out.rglob("*") if f.is_file() and f.suffix != ".zip"}
        differ = sorted(k for k in released.keys() & regen.keys()
                        if normalised(released[k]) != normalised(regen[k]))
        only = sorted(released.keys() ^ regen.keys())
        if differ or only:
            say("FAIL", "re-derive: package", f"differs: {differ}; present on one side "
                f"only: {only}")
        else:
            say("PASS", "re-derive: package", f"{len(released)} files regenerate identically "
                "(creation dates aside)")
        net = next(work.glob("*-netlist.kicad_net"), None)
        v = subprocess.run([sys.executable, str(HERE / "validate_gerbers.py"),
                            str(rel_dir / "fab"), "-c", str(work / "board.toml"),
                            "--no-color"] + (["--kicad-netlist", str(net)] if net else []),
                           capture_output=True, text=True)
        say("PASS" if v.returncode == 0 else "FAIL", "re-derive: validate_gerbers",
            "" if v.returncode == 0 else v.stdout.strip()[-1500:])


def main() -> int:
    ap = argparse.ArgumentParser(prog="verify_release")
    ap.add_argument("release", help="release/rev-N directory")
    args = ap.parse_args()
    rel_dir = Path(args.release)
    if not rel_dir.is_dir():
        print(f"not a directory: {rel_dir}", file=sys.stderr)
        return 1
    level_hashes(rel_dir)
    level_zip(rel_dir)
    level_rederive(rel_dir)
    print(f"\n{failures} failed, {skips} not checked")
    if failures:
        return 1
    return 2 if skips else 0


if __name__ == "__main__":
    raise SystemExit(main())
