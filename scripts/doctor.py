#!/usr/bin/env python3
"""
doctor.py - check that this machine can actually run the pipeline.

Five things have to line up, and each one fails in a way that wastes an
afternoon if you don't know what you're looking at. This names the problem
and the fix.

    python3 scripts/doctor.py

Exit code non-zero if anything is missing.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

OK, BAD, WARN = "OK", "MISSING", "WARN"
MIN_KICAD = 9
MIN_PY = (3, 11)


def sh(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:
        return 1, repr(exc)


class Doc:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.os = platform.system()

    def add(self, status: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((status, name, detail, fix))

    @property
    def broken(self) -> int:
        return sum(1 for r in self.rows if r[0] == BAD)

    def render(self) -> str:
        color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"

        def paint(s: str, c: str) -> str:
            if not color:
                return s
            return f"\033[{ {'g':'32','r':'31','y':'33','d':'2'}[c] }m{s}\033[0m"

        w = max(len(r[1]) for r in self.rows)
        out = []
        for status, name, detail, fix in self.rows:
            tag = {
                OK: paint("  ok    ", "g"),
                BAD: paint("MISSING ", "r"),
                WARN: paint(" warn   ", "y"),
            }[status]
            out.append(f"{tag}{name.ljust(w)}  {detail}")
            if fix and status != OK:
                for line in fix.strip().splitlines():
                    out.append(paint(f"          {line}", "d"))
        return "\n".join(out)


# --------------------------------------------------------------------------


def check_python(doc: Doc) -> None:
    v = sys.version_info
    if v >= MIN_PY:
        doc.add(OK, "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        doc.add(
            BAD,
            "python",
            f"{v.major}.{v.minor}, need {MIN_PY[0]}.{MIN_PY[1]}+",
            "The checker uses tomllib, which landed in 3.11.",
        )


def check_kicad_cli(doc: Doc) -> None:
    exe = shutil.which("kicad-cli")
    if not exe:
        fixes = {
            "Darwin": "brew install --cask kicad\n"
            "then add /Applications/KiCad/KiCad.app/Contents/MacOS to PATH",
            "Linux": "sudo add-apt-repository ppa:kicad/kicad-9.0-releases\n"
            "sudo apt install kicad",
            "Windows": "winget install KiCad.KiCad",
        }
        doc.add(BAD, "kicad-cli", "not on PATH", fixes.get(doc.os, "install KiCad 9"))
        return
    rc, out = sh(["kicad-cli", "--version"])
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and int(m.group(1)) >= MIN_KICAD:
        doc.add(OK, "kicad-cli", f"{m.group(0)}  ({exe})")
    elif m:
        doc.add(
            BAD,
            "kicad-cli",
            f"{m.group(0)}, need {MIN_KICAD}+",
            "Layer names and CLI flags changed in 9. Older versions will "
            "fail in confusing ways rather than refusing to run.",
        )
    else:
        doc.add(WARN, "kicad-cli", f"version unreadable: {out[:60]}")


def check_pcbnew(doc: Doc) -> None:
    """The bundled Python, not necessarily the one running this script."""
    candidates: list[str] = []
    if doc.os == "Darwin":
        candidates += [
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
            "Versions/Current/bin/python3"
        ]
    candidates += [sys.executable, "python3"]

    for exe in candidates:
        if not Path(exe).exists() and not shutil.which(exe):
            continue
        rc, out = sh([exe, "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"])
        if rc == 0 and out:
            note = "" if exe == sys.executable else f"  (use: {exe})"
            doc.add(OK, "pcbnew", f"{out.splitlines()[-1]}{note}")
            return
    fixes = {
        "Darwin": "KiCad ships its own Python. Run build scripts with:\n"
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
        "Python.framework/Versions/Current/bin/python3",
        "Linux": "sudo apt install kicad-python  (or use the KiCad flatpak's python)",
    }
    doc.add(
        BAD,
        "pcbnew",
        "not importable from any Python found",
        fixes.get(doc.os, "")
        + "\nThis is the #1 setup failure: pcbnew only imports from KiCad's\n"
        "own interpreter, not from a venv or a brew/apt python.",
    )


def check_java(doc: Doc) -> None:
    exe = shutil.which("java")
    if not exe:
        doc.add(
            BAD,
            "java",
            "not on PATH",
            "Freerouting needs a JVM. Install a JDK 21 build:\n"
            "  macOS:  brew install openjdk@21\n"
            "  Ubuntu: sudo apt install openjdk-21-jre",
        )
        return
    rc, out = sh(["java", "-version"])
    m = re.search(r'"?(\d+)[.\"]', out)
    if m:
        major = int(m.group(1))
        if major >= 21:
            doc.add(OK, "java", f"{major}")
        else:
            doc.add(
                WARN,
                "java",
                f"{major}",
                "Newer Freerouting releases need a newer JVM. Pin the "
                "Freerouting version to this Java, or upgrade Java.",
            )
    else:
        doc.add(WARN, "java", f"version unreadable: {out.splitlines()[0][:50]}")


def check_freerouting(doc: Doc) -> None:
    env = os.environ.get("FREEROUTING_JAR")
    search = [Path(env)] if env else []
    search += list(Path("tools").glob("freerouting*.jar"))
    search += list(Path.home().glob("freerouting*/freerouting*.jar"))
    for p in search:
        if p.exists():
            doc.add(OK, "freerouting", str(p))
            return
    doc.add(
        BAD,
        "freerouting",
        "jar not found",
        "Download a release jar from github.com/freerouting/freerouting\n"
        "into ./tools/, or set FREEROUTING_JAR to its path.\n"
        "Pin the version — and run it with -mt 1, in the foreground.\n"
        "Backgrounded, it opens the file and exits without routing.",
    )


def check_uv(doc: Doc) -> None:
    exe = shutil.which("uv")
    if exe:
        rc, out = sh(["uv", "--version"])
        doc.add(OK, "uv", out.strip() or exe)
    else:
        doc.add(
            WARN,
            "uv",
            "not on PATH",
            "Optional but recommended, it pins the Python environment:\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh",
        )


def check_git(doc: Doc) -> None:
    if shutil.which("git"):
        doc.add(OK, "git", sh(["git", "--version"])[1])
    else:
        doc.add(BAD, "git", "not on PATH", "You need history. Install git.")


def check_skills(doc: Doc) -> None:
    """kicad-happy installs as a Claude Code plugin, so look for its marker."""
    homes = [
        Path.home() / ".claude" / "plugins",
        Path.home() / ".config" / "claude" / "plugins",
    ]
    for h in homes:
        if h.exists() and any("kicad-happy" in p.name for p in h.rglob("*")):
            doc.add(OK, "kicad-happy", f"installed under {h}")
            return
    doc.add(
        WARN,
        "kicad-happy",
        "not detected",
        "In Claude Code:\n"
        "  /plugin marketplace add aklofas/kicad-happy\n"
        "  /plugin install kicad-happy@kicad-happy\n"
        "Detection is best-effort; ignore this if /kicad works.",
    )


CHECKS = [
    check_python,
    check_git,
    check_kicad_cli,
    check_pcbnew,
    check_java,
    check_freerouting,
    check_uv,
    check_skills,
]


def main() -> int:
    doc = Doc()
    print(f"\npcb-agent doctor — {platform.system()} {platform.machine()}\n")
    for c in CHECKS:
        try:
            c(doc)
        except Exception as exc:
            doc.add(WARN, c.__name__, f"check crashed: {exc!r}")
    print(doc.render())
    if doc.broken:
        print(f"\n{doc.broken} thing(s) to fix before the pipeline will run.\n")
        return 1
    print("\nEverything the pipeline needs is here.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
