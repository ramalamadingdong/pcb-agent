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

# The build container (see Dockerfile / run.sh). When the image is present,
# kicad/pcbnew/java/freerouting missing from the host is not a problem — the
# Makefile runs those stages inside the container.
IMAGE = os.environ.get("PCB_AGENT_IMAGE", "pcb-agent")
IN_CONTAINER = os.environ.get("PCB_AGENT_CONTAINER") == "1"


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
        # Filled by check_container when the image is usable:
        # {"kicad_cli": "9.0.9", "java": "...", "jar": "/opt/...", ...}
        self.container: dict[str, str] | None = None
        # Filled by check_java so check_freerouting can pair jar <-> JVM.
        self.java_major: int | None = None

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


def check_container(doc: Doc) -> None:
    """Probe the build container once; later checks consult the result.

    One `docker run` answers everything — versions read from inside the
    container, not assumed from the Dockerfile that built it.
    """
    if IN_CONTAINER:
        doc.add(OK, "container",
                "running inside the image — rows below are the container's own tools")
        return
    if not shutil.which("docker"):
        doc.add(WARN, "container", "docker not on PATH",
                "Optional — native tools work too. With docker installed,\n"
                "`make setup` builds an image carrying KiCad, Java and\n"
                "Freerouting so none of them need installing by hand.")
        return
    rc, _ = sh(["docker", "image", "inspect", IMAGE])
    if rc != 0:
        doc.add(WARN, "container", f"image '{IMAGE}' not built",
                "`make setup` builds it (or the docker daemon isn't running).")
        return
    probe = (
        'echo "kicad_cli=$(kicad-cli --version 2>/dev/null | head -1)"; '
        'echo "java=$(java -version 2>&1 | head -1)"; '
        '[ -f "$FREEROUTING_JAR" ] && echo "jar=$FREEROUTING_JAR"; '
        'echo "pcbnew=$(python3 -c \'import pcbnew; print(pcbnew.GetBuildVersion())\' 2>/dev/null)"; '
        'echo "uv=$(uv --version 2>/dev/null)"'
    )
    rc, out = sh(["docker", "run", "--rm", IMAGE, "sh", "-c", probe], timeout=60)
    if rc != 0:
        doc.add(WARN, "container", f"image '{IMAGE}' present but won't run: {out[:60]}")
        return
    info: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if v.strip():
                info[k.strip()] = v.strip()
    doc.container = info
    doc.add(OK, "container",
            f"image '{IMAGE}'  (kicad-cli {info.get('kicad_cli', '?')}, "
            f"freerouting {'yes' if 'jar' in info else 'MISSING'})")


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
    if not exe and doc.container:
        doc.add(OK, "kicad-cli",
                f"not on host; container provides {doc.container.get('kicad_cli', '?')}. "
                "Install KiCad natively too when you want to LOOK at the board.")
        return
    if not exe:
        fixes = {
            "Darwin": "brew install --cask kicad\n"
            "then add /Applications/KiCad/KiCad.app/Contents/MacOS to PATH",
            "Linux": "sudo add-apt-repository ppa:kicad/kicad-10.0-releases\n"
            "sudo apt install kicad   (match the major your boards use)",
            "Windows": "winget install KiCad.KiCad",
        }
        doc.add(BAD, "kicad-cli", "not on PATH", fixes.get(doc.os, "install KiCad 9"))
        return
    rc, out = sh(["kicad-cli", "--version"])
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and int(m.group(1)) >= MIN_KICAD:
        # Host and container drifting apart is the failure mode the container
        # design creates: the board you review natively is not quite the
        # board the container built. Warn on a major-version gap.
        cver = (doc.container or {}).get("kicad_cli", "")
        cm = re.match(r"(\d+)\.", cver)
        if cm and int(cm.group(1)) != int(m.group(1)):
            doc.add(
                WARN,
                "kicad-cli",
                f"host {m.group(0)} vs container {cver} — major versions differ",
                "The container builds the board; the host is what you review\n"
                "it with. A major-version gap means file-format and behaviour\n"
                "drift. Rebuild the image or change the host KiCad to match.",
            )
        else:
            doc.add(OK, "kicad-cli", f"{m.group(0)}  ({exe})")
    elif m and doc.container:
        doc.add(
            WARN,
            "kicad-cli",
            f"host {m.group(0)} is older than {MIN_KICAD}; container provides "
            f"{doc.container.get('kicad_cli', '?')}",
            "The container runs the pipeline, but this host KiCad may not\n"
            "even open the boards it produces. Upgrade the host KiCad.",
        )
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
    if doc.container and doc.container.get("pcbnew"):
        doc.add(OK, "pcbnew",
                f"not importable on host; container provides {doc.container['pcbnew']}")
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


def check_board_format(doc: Doc) -> None:
    """The drift that actually bites is the board FORMAT, not the CLI major.

    A KiCad 9 pcbnew loading a (version 20260206) board written by KiCad 10
    returns None — no exception, no error. Comparing kicad-cli versions
    would miss a same-major format bump, so when a board exists, actually
    try to load it with whatever pcbnew will process it.
    """
    boards = sorted(Path(".").glob("*.kicad_pcb")) + sorted(
        Path(".").glob("examples/*/*.kicad_pcb")
    )
    if not boards:
        return  # nothing to check yet — no row, no noise
    board = boards[0]
    m = re.search(r"\(version\s+(\d+)\)", board.read_text(errors="replace")[:2000])
    ver = m.group(1) if m else "?"

    loader = (
        "import pcbnew,shutil; shutil.copy('{src}', '/tmp/_fmt.kicad_pcb'); "
        "b = pcbnew.LoadBoard('/tmp/_fmt.kicad_pcb'); "
        "print('LOADED' if b else 'NONE')"
    )
    if IN_CONTAINER:
        rc, out = sh([sys.executable, "-c", loader.format(src=board)], timeout=60)
    elif doc.container and shutil.which("docker"):
        rc, out = sh(
            [
                "docker", "run", "--rm",
                "-v", f"{board.resolve().parent}:/fmt:ro",
                "-e", "HOME=/tmp",
                IMAGE, "python3", "-c", loader.format(src=f"/fmt/{board.name}"),
            ],
            timeout=90,
        )
    else:
        doc.add(
            WARN,
            "board-format",
            f"{board.name} is format {ver} — no container/pcbnew here to test-load it",
        )
        return
    if rc == 0 and "LOADED" in out:
        doc.add(OK, "board-format", f"{board.name} (format {ver}) loads in pcbnew")
    elif "NONE" in out:
        doc.add(
            BAD,
            "board-format",
            f"pcbnew returns None loading {board.name} (format {ver})",
            "The board was written by a newer KiCad than the one trying to\n"
            "read it. Rebuild the container against the KiCad that wrote it\n"
            "(KICAD_BASE in the Makefile), or re-export from an older KiCad.",
        )
    else:
        doc.add(WARN, "board-format", f"load test failed to run: {out[:60]}")


def check_java(doc: Doc) -> None:
    exe = shutil.which("java")
    if not exe and doc.container and doc.container.get("java"):
        doc.add(OK, "java", f"not on host; container provides {doc.container['java']}")
        return
    if not exe:
        doc.add(
            BAD,
            "java",
            "not on PATH",
            "Freerouting needs a JVM — 25+ for the pinned 2.x release\n"
            "(Freerouting 2.3.0 is compiled for class-file 69 = Java 25):\n"
            "  macOS:  brew install temurin@25\n"
            "  Ubuntu: install a Temurin 25 JRE from adoptium.net",
        )
        return
    rc, out = sh(["java", "-version"])
    m = re.search(r'"?(\d+)[.\"]', out)
    if m:
        major = int(m.group(1))
        doc.java_major = major
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
            # Pair the jar with the JVM found earlier. Freerouting 2.x is
            # compiled for Java 25 (class-file 69); a Java 21 that happily
            # runs 1.9.0 dies on 2.3.0 with UnsupportedClassVersionError.
            jm = re.search(r"freerouting-(\d+)\.", p.name)
            jar_major = int(jm.group(1)) if jm else None
            java_major = getattr(doc, "java_major", None)
            if jar_major and jar_major >= 2 and java_major and java_major < 25:
                doc.add(
                    WARN,
                    "freerouting",
                    f"{p} needs Java 25+, host java is {java_major}",
                    "Freerouting 2.x is compiled for Java 25. Upgrade the JVM\n"
                    "or run routing in the container (which carries both).",
                )
            else:
                doc.add(OK, "freerouting", str(p))
            return
    if doc.container and doc.container.get("jar"):
        doc.add(OK, "freerouting",
                f"not on host; container provides {doc.container['jar']}")
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
    """Best-effort: Claude Code's plugin layout varies by version/OS.

    Observed installs (2026-08): a ~/.claude/kicad-happy/ directory plus the
    individual skills under ~/.claude/skills/<name>/ — NOT under
    ~/.claude/plugins, which held only a blocklist. Check all of them.
    """
    claude = Path.home() / ".claude"
    homes = [
        claude / "plugins",
        Path.home() / ".config" / "claude" / "plugins",
    ]
    for h in homes:
        if h.exists() and any("kicad-happy" in p.name for p in h.rglob("*")):
            doc.add(OK, "kicad-happy", f"installed under {h}")
            return
    if (claude / "kicad-happy").exists():
        doc.add(OK, "kicad-happy", f"installed at {claude / 'kicad-happy'}")
        return
    skills = claude / "skills"
    known = {"kicad", "lcsc", "jlcpcb", "digikey", "mouser", "emc", "spice"}
    if skills.exists():
        present = known & {p.name for p in skills.iterdir() if p.is_dir()}
        if len(present) >= 3:
            doc.add(OK, "kicad-happy",
                    f"{len(present)} of its skills under {skills}")
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
    check_container,  # first — later checks consult its probe
    check_python,
    check_git,
    check_kicad_cli,
    check_pcbnew,
    check_board_format,
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
