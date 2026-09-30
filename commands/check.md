---
description: Validate the schematic, the board and its exported fab package against board.toml. Run after every export, and always before ordering.
---

`make check` runs five gates. Each one runs even if an earlier one fails,
and `check` fails at the end if any did. They read different artifacts and
fail differently, so no one of them passing excuses skipping another.

| gate | reads | catches |
| --- | --- | --- |
| `erc_check.py` | schematic | ERC errors not in `[[erc.accept]]` (level 1) |
| `fix_pad_angles.py --check` | board | pads not at footprint rotation + library angle: the "pads on a rotated part lie along its axis" fault |
| `drc_sample.py` | board (a filled **copy**) | DRC ×5: any error not in `[[drc.accept]]`, any unconnected item, any parity issue about connectivity, any GUI exclusion, any stale ledger entry (level 2) |
| `check_placement.py` | board | connectors a person can't use, and copper in keepouts |
| `validate_gerbers.py` | the fab package, as text | what the fab will plot, **including the IPC-D-356 netlist held to netlist.csv** (levels 0 and 4 on what ships) |

(`make erc`, `make drc` and `make check-placement` run one gate each.)

- **`check_placement.py`** reads the BOARD through pcbnew and can name what
  it found (`R12 pad 2`, `via GND at 41.20,18.05`). For every
  `[[connectors]]` part it checks:
  - **which way it actually faces.** This is derived from its rotation by
    fitting the part's pads to the library footprint, then comparing the
    footprint's datasheet opening (`[connector_footprints]`) with the
    declared face. A connector rotated 180° at the right edge passes every
    clearance test while its opening points into the board.
  - **whether its mating face reaches the outline,** and whether the
    corridor the plug and cable need is clear. Mounting holes count at the
    screw head's size.
  - **for vertical and stacking headers,** whether an access ring on the
    mating side is clear.
  - **whether every connector-looking ref (J/P/CN/USB) is declared** at all.
- **`validate_gerbers.py`** reads the exported bytes as text, with no
  dependencies and no KiCad. It cannot name anything beyond apertures and
  flashes, but those are the bytes the fab will plot.

A PASS on the board and a FAIL on the package means the **export** moved
something. That case is why both exist.

Report every line. Then:

- **Any FAIL:** fix it in the pipeline, never in the board file. Re-run the
  build, re-export, re-check. A fix made by hand is wiped by the next
  rebuild. A connector failure is usually a floorplan fix
  (`[[floorplan.place]]`), not a routing one.
- **A ledger entry (`[[drc.accept]]`, `[[erc.accept]]`, `accept_blockers`)
  is a claim, and it needs a reason a reviewer would accept.** Never add one
  just to turn a gate green. If you can't write down why the violation is
  fine, it isn't.
- **Any SKIP:** that is a check nobody wrote. Say so plainly. A rule you
  never configured is not a rule that passed.
- **Warnings** (DRC silk, parity lib-nickname mismatches, ERC footprint
  links) are printed but don't gate. Read them anyway. Don't wave off a
  warning because a gate stayed green.
- **All PASS:** good, and still not evidence the board works. Done and clean
  and fabricated isn't the same as working.

Then look at the board: `make render` writes one PNG per copper layer to
`review/`. Read them. That's level 3, and it's the one to trust least,
because you'll see what the build was supposed to do. Look for what it did.

If the user found something by hand that the checker missed, add it to
`board.toml` now, while you know what it was. That is how this file earns its
keep.
