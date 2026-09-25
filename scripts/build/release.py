#!/usr/bin/env python3
"""release — freeze the reviewed project and the ordered package together.

A reviewer who opens "the project" must be looking at the files the fab
package was plotted from, not a rebuild that might look the same. This pass
copies exactly those files into

    <board dir>/release/rev-<[build] revision>/
        <name>.kicad_pro / .kicad_sch / .kicad_pcb   what the reviewer opens
        fp-lib-table, sym-lib-table, project libraries
        netlist.csv, board.toml (+ [fab.jlc] bom_csv)
        <name>-netlist.kicad_net, <name>-parity.json  the gate's evidence
        fab/                                          the package that ships
        release.json                                  git commit, KiCad version
        MANIFEST.sha256                               every file above

and refuses to write it unless, right now, on these bytes:
  * check_parity passes (the board is its schematic), and
  * validate_gerbers passes on fab/, pad nets included.

A release is immutable. Re-running with nothing changed is a no-op; a
changed board under the same revision fails -- bump [build] revision. Commit
the directory: "save the exact files you order" means these, and
`scripts/verify_release.py` proves later that they still agree.

Reviewers open release/rev-N/<name>.kicad_pro and COMMENT; they never save.
A save changes a hash and verify_release says so.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

NAME = "release"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest_of(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in ("MANIFEST.sha256", "release.json"):
            out[p.relative_to(root).as_posix()] = sha256(p)
    return out


def git_state() -> dict:
    def g(*a: str) -> str | None:
        try:
            p = subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)
        except FileNotFoundError:
            return None
        return p.stdout.strip() if p.returncode == 0 else None
    # The release directory being written is not "uncommitted work".
    status = g("status", "--porcelain", "--", ".", ":(exclude,glob)**/release/**")
    return {"commit": g("rev-parse", "HEAD"),
            "dirty": None if status is None else bool(status)}


def run_step(cmd: list[str], what: str) -> None:
    log("+ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if p.stdout:
        log(p.stdout.rstrip())
    if p.returncode != 0:
        _lib.fail(f"release refused: {what} failed")


def stage(board: Path, cfg: dict, cfg_path: Path, fab_dir: Path, dest: Path) -> None:
    """Copy everything a reviewer and the fab need into dest, at the same
    paths relative to each other (the lib tables are ${KIPRJMOD}-relative)."""
    here = board.parent
    stem = board.stem

    def put(src: Path, rel: str | None = None) -> None:
        if not src.exists():
            _lib.fail(f"{src}: missing — cannot release without it")
        target = dest / (rel or src.relative_to(here).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)

    for suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        put(board.with_suffix(suffix))
    for extra in (f"{stem}-netlist.kicad_net", f"{stem}-parity.json",
                  "fp-lib-table", "sym-lib-table"):
        put(here / extra)
    # Whatever the lib tables name, via ${KIPRJMOD}.
    for table in ("fp-lib-table", "sym-lib-table"):
        for tok in (here / table).read_text(encoding="utf-8").split('"'):
            if tok.startswith("${KIPRJMOD}/"):
                put(here / tok[len("${KIPRJMOD}/"):])
    put(cfg_path.resolve(), "board.toml")
    netlist = cfg_path.resolve().parent / "netlist.csv"
    put(netlist, "netlist.csv")
    jlc = (cfg.get("fab") or {}).get("jlc") or {}
    if jlc.get("bom_csv"):
        put(cfg_path.resolve().parent / jlc["bom_csv"], jlc["bom_csv"])
    shutil.copytree(fab_dir, dest / "fab")


def main() -> int:
    ap = _lib.pass_parser(NAME, board=True, netlist=True)
    ap.add_argument("--fab", default=None,
                    help="fab package dir (default [fab].out_dir or 'fab' beside the config)")
    args = ap.parse_args()

    board = Path(args.board)
    cfg_path = Path(args.config)
    cfg = _lib.load_config(cfg_path)
    if not cfg:
        _lib.fail(f"{cfg_path}: config not found or empty")
    rev = str((cfg.get("build") or {}).get("revision") or "").strip()
    if not rev:
        _lib.fail("board.toml needs [build] revision — a release is named by it")
    fab_cfg = cfg.get("fab") or {}
    fab_dir = Path(args.fab) if args.fab else cfg_path.resolve().parent / fab_cfg.get("out_dir", "fab")
    if not any(fab_dir.glob("*.gbr")):
        _lib.fail(f"{fab_dir}: no gerbers — run `make export` first")

    # Evidence first, on the bytes being frozen.
    run_step([sys.executable, str(HERE / "check_parity.py"), "--board", str(board),
              "--config", str(cfg_path)], "check_parity")
    net = board.with_name(f"{board.stem}-netlist.kicad_net")
    run_step([sys.executable, str(REPO / "scripts" / "validate_gerbers.py"), str(fab_dir),
              "-c", str(cfg_path), "--kicad-netlist", str(net), "--no-color"],
             "validate_gerbers")

    dest = board.parent / "release" / f"rev-{rev}"
    # Before staging: the staging directory lives beside the board and would
    # itself read as uncommitted work.
    git = git_state()
    with tempfile.TemporaryDirectory(dir=board.parent) as t:
        staged = Path(t) / "stage"
        stage(board, cfg, cfg_path, fab_dir, staged)
        want = manifest_of(staged)
        if dest.exists():
            have = manifest_of(dest)
            if have == want:
                log(f"  {dest} already holds exactly these files — nothing to do")
                _lib.emit(NAME, release=str(dest), revision=rev, files=len(want), wrote=False)
                return 0
            changed = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
            _lib.fail(f"{dest} already exists with different content ({len(changed)} file(s), "
                      f"e.g. {changed[:5]}). A release is immutable: bump [build] revision.")
        (staged / "MANIFEST.sha256").write_text(
            "".join(f"{h}  {p}\n" for p, h in want.items()), encoding="utf-8")
        parity = json.loads((staged / f"{board.stem}-parity.json").read_text(encoding="utf-8"))
        (staged / "release.json").write_text(json.dumps({
            "revision": rev,
            "project": f"{board.stem}.kicad_pro",
            "git": git,
            "kicad_version": parity.get("kicad_version"),
            "parity_ok": parity.get("ok"),
            "open_this": f"{board.stem}.kicad_pro",
            "note": "Review only. Do not save from KiCad; verify with "
                    "scripts/verify_release.py. Changes go in netlist.csv / board.toml "
                    "and a new revision.",
        }, indent=2) + "\n", encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(dest))

    log(f"  released {len(want)} files -> {dest}")
    _lib.emit(NAME, release=str(dest), revision=rev, files=len(want), wrote=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
