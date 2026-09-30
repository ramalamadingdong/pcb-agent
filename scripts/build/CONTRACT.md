# The pass contract

Every script in this directory is a **pass**: one step of
`netlist.csv -> build -> board -> snapshot -> route -> gerbers -> check`.
They are run by the Makefile and by `route.py` / `place.py` as subprocesses,
never imported for their side effects. This file is what every pass
promises, so that a new one can be written against it and an old one can be
replaced (a SWIG pass by a `kicad-cli` or IPC pass, say) without the rest of
the pipeline noticing.

## Arguments

Built by `_lib.pass_parser`:

| flag | meaning |
| --- | --- |
| `--board PATH` | the `.kicad_pcb` the pass reads and (usually) rewrites in place |
| `--schematic PATH` | instead of, or as well as, `--board` for schematic passes |
| `--netlist PATH` | `netlist.csv`, the source of truth. Accepted by every pass for uniformity, even those that ignore it |
| `--config PATH` | `board.toml`. Every tunable lives here, never in a pass's source |

A pass may add its own flags (`place.py --rounds`, `route.py --snapshot`).
It must not require a flag the Makefile doesn't pass.

## Output

- **stdout carries exactly one line: a JSON object** from `_lib.emit(name, ...)`,
  with `"pass": <name>` and whatever the pass changed or measured. The
  agent reads this line to confirm what actually happened instead of assuming.
  Callers (`route.py::run_pass`) take the *last* line that parses as JSON.
- **Everything else goes to stderr.** That includes progress, warnings, and
  PASS/FAIL tables.
- A checker that fails still emits its JSON line first, then exits non-zero
  (`check_placement.py`, `drc_sample.py`, `erc_check.py`). The report is the
  evidence, and a failure without evidence is useless.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | the pass did its job (a checker: nothing unaccepted failed) |
| 1 | failure. The board may be half-written; rebuild from an earlier stage |
| other | pass-specific and documented in the pass. `finish_routes.py` exits 1 for "nets remain unfinished", which `route.py` allows and reports |

`_lib.fail(msg)` prints to stderr and exits 1. Use it for anything the user has
to fix. Never swallow an error and exit 0.

## Idempotency

**Running a pass twice must give the same board as running it once.** You
will re-run a stage when you aren't sure it ran; an appending pass turns that
into junk copper.

- **Strip, then rebuild.** A pass that adds objects (fiducials, keepouts,
  stitching vias, silk) first removes what it previously added, identified by
  a name or group it owns, then adds.
- **Assign absolutely, never relatively.** `floorplan.py` writes positions,
  never offsets, and unlocks every ref that isn't in its table.
- **No randomness in written files.** `generate_schematic.py` seeds uuid4
  (`deterministic_uuids`) so an unchanged netlist gives a byte-identical
  schematic.
- **Stochastic passes are restartable, not repeatable.** `place.py` (CMA-ES)
  and `route.py` (Freerouting) can't be byte-identical. Instead they start
  from a fixed input (the board as it stands, or `pre_route.kicad_pcb`), never
  append, and keep the best measured result.

## Writes

- Passes rewrite `--board` in place. Text writes go through
  `_lib.write_text_atomic` (a sibling temp file, then `os.replace`), so a
  crash mid-save leaves the previous file, never a truncated one.
  `pcbnew.SaveBoard` writes are **not** atomic, and that is deliberate:
  saving to a temp name also writes `<temp>.kicad_pro` / `.kicad_prl` and
  stamps the temp name into the project's `meta.filename` (measured on KiCad
  10.0.5). A crashed SWIG pass means rebuilding from the previous stage.
- **Assert the net table after every board write** (`_lib.assert_net_table`).
  One KiCad write path drops the top-level net table. Everything still opens,
  and the router then routes nothing.
- A pass that runs an optimiser runs `repair_pads.py` after it, every time
  (see CLAUDE.md).
- `route.py` never modifies its `--snapshot`. It copies it over `--board` and
  starts from there.

## Interpreters

A pass imports one of two KiCad APIs, and that decides which Python runs it:

| API | passes |
| --- | --- |
| `pcbnew` (SWIG, KiCad's bundled Python) | add_fiducials, add_keepouts, check_placement, cleanup_pass, export_dsn, export_fab, finish_routes, fix_pad_angles, flip_sides, import_ses, post_route_fix, silk_finish, zones, drc_sample (zone fill), score_route |
| `kicad_tools` (`kct`) | fanout, floorplan, generate_schematic, make_libs, tuck_in |
| neither (text / subprocess) | add_mounting_holes, apply_netclasses, create_pcb, place, repair_pads, erc_check |

`place.py` runs its siblings under `KCT_PYTHON` / `KICAD_PYTHON` for this
reason. `kicad-cli` is invoked as `$KICAD_CLI`, split like a shell word list.

## Coordinates

`board.toml` uses one frame: origin at the outline's **bottom-left**, X right,
**Y up**. KiCad pages run Y down. Convert with `_lib.board_frame` /
`_lib.to_kicad_xy`, and never write a config coordinate straight into pcbnew.

## Checkers

Checkers are passes that write nothing. They report PASS / FAIL / SKIP and
follow one rule for exceptions: an **accepted** item is still measured,
still printed as `accepted:`, and still in the JSON. It is downgraded, never
hidden. **An acceptance that matches nothing is itself a failure**, because a
stale acceptance is a check that succeeds forever. See `accept_blockers` in
`check_placement.py`, and `[[drc.accept]]` / `[[erc.accept]]` in
`drc_sample.py` / `erc_check.py`.
