# rev-<N> plan

**Status:** draft | in build | ordered
**Date:**

## Scope
What this revision is for, in one paragraph.

## Defect it fixes
If this is a respin: what was wrong with the last one, how it was found, and
the change that addresses it. If a checker missed it, note the rule added to
`board.toml` so it can't be missed again.

## Requirements
From firmware where it exists, every line citing file and line number.
Mark `ASSUMED` where there is no firmware yet.

| # | Requirement | Source |
| - | ----------- | ------ |

## Parts
| RefDes | Part | Price | Stock | Lead | Why | Datasheet |
| ------ | ---- | ----- | ----- | ---- | --- | --------- |

Rejected alternatives and why:

## Capacity math
- Data rate vs storage:
- Current vs trace width (IPC-2221, __ oz copper):
- Dissipation vs package:
- Battery capacity vs average draw:

Numbers that changed from the last revision, and why:

## Gates — all must pass before ordering
- [ ] Schematic round-trip diff clean against `netlist.csv`
- [ ] ERC clean
- [ ] DRC run 5+ times, comparing which violations appear
- [ ] `check_parity` PASS on the final board (schematic is the board that ships)
- [ ] `validate_gerbers` PASS, pad nets included (`--kicad-netlist`)
- [ ] Every part in stock, re-verified at order time
- [ ] `make release` committed; `make verify-release` PASS; reviewers opened `release/rev-N/`
- [ ] (board-specific gates here)
