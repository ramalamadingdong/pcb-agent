#!/usr/bin/env python3
"""Import the router's Specctra SES back onto the board.

A thin wrapper on `pcbnew.ImportSpecctraSES` — the whole pass is that one
call plus the before/after track count that proves it did something.

TRAP, and the reason this runs late in the pipeline and never early:
`ImportSpecctraSES` + `SaveBoard` rewrites the board into the name-only net
dialect — the top-level `(net <id> "<name>")` table goes from every net to
zero.  KiCad, DRC and every fab export read this fine, but tools with their
own board parser cannot, so run this AFTER the build passes, never before.
That is also why nothing downstream of here asserts the net table: on this
path an empty one is the expected, documented state, not a fault.

board.toml keys consumed: none.

Not ported: nothing — the source pass was this call and its prints.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import count_segments, emit, fail, pass_parser  # noqa: E402


def log(*a) -> None:
    print(*a, file=sys.stderr)


def main() -> int:
    ap = pass_parser("import_ses")
    ap.add_argument("--ses", required=True, help="path to the router's .ses")
    ap.add_argument("--out", help="board to write (default: in place, --board)")
    args = ap.parse_args()

    out = args.out or args.board
    if not Path(args.ses).exists():
        fail(f"{args.ses}: no such session file — the router wrote nothing")

    b = pcbnew.LoadBoard(args.board)
    if b is None:
        fail(f"pcbnew could not load {args.board} (KiCad major mismatch?)")
    before = len(b.GetTracks())
    log("before: tracks", before)

    ok = pcbnew.ImportSpecctraSES(b, args.ses)
    log("ImportSpecctraSES ->", ok)
    if not ok:
        fail(f"ImportSpecctraSES failed reading {args.ses}")

    after = len(b.GetTracks())
    log("after : tracks", after)
    pcbnew.SaveBoard(out, b)
    log("saved", out)

    emit(
        "import_ses",
        board=out,
        ses=args.ses,
        tracks_before=before,
        tracks_after=after,
        segments=count_segments(out),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
