---
description: Validate the board and its exported fab package against board.toml. Run after every export, and always before ordering.
---

Two checkers, run both. They read different artifacts and fail differently,
and neither one passing excuses skipping the other.

```bash
python3 scripts/build/check_placement.py --board <board>.kicad_pcb --config board.toml
python3 scripts/validate_gerbers.py ./fab/ -c board.toml
```

(`make check` runs both, in that order.)

- **`check_placement.py`** reads the BOARD through pcbnew. It checks that
  every `[[connectors]]` part can actually be plugged into — its mating face
  reaches the outline, and nothing sits in the corridor the plug and its cable
  need (mounting holes inflated to the screw head, which is the part actually
  in the way) — and that no track, via, pad or pour has landed inside a
  `[[keepouts]]` rectangle. It can name what it found: `R12 pad 2`,
  `via GND at 41.20,18.05`.
- **`validate_gerbers.py`** reads the exported bytes as text, with no
  dependencies and no KiCad. It cannot name anything — only apertures and
  flashes — but those are the bytes the fab will plot.

A PASS in the first and a FAIL in the second means the **export** moved
something. That case is why both exist.

Report every line. Then:

- **Any FAIL** — fix it in the pipeline, never in the board file. Re-run the
  build, re-export, re-check. A fix made by hand is wiped by the next rebuild.
  A connector failure is usually a floorplan fix (`[[floorplan.place]]`), not
  a routing one.
- **Any SKIP** — that is a check nobody wrote. Say so plainly. A rule you
  never configured is not a rule that passed. `connectors: no [[connectors]]
  declared` means nothing on this board has ever been checked for whether a
  plug fits.
- **All PASS** — good, and still not evidence the board works. Done and clean
  and fabricated isn't the same as working.

If the user found something by hand that the checker missed, add it to
`board.toml` now, while you know what it was. That is how this file earns its
keep.
