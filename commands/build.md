---
description: Build the board from netlist.csv — schematic, PCB, placement, zones, fanout, silk. Run after any netlist change.
---

Run `make build`.

Before you start, confirm `scripts/doctor.py` exits clean. A missing `pcbnew`
or the wrong KiCad fails in ways that look like design problems.

Watch for three things and stop if you see any of them:

- **The round-trip check failing.** Every net in `netlist.csv` must come back
  out of the generated schematic. This is the check that catches a label a
  fraction of a millimetre off a pin — a schematic that looks perfect and
  carries no net.
- **An empty net table** after any board write. `grep -cE '^\s*\(net [0-9]+ '`
  must be non-zero. If it's zero the router will report "nets to route: 0"
  and do nothing, with no error anywhere.
- **`link_schematic` refusing a footprint.** It names any footprint with no
  schematic symbol that `board.toml` doesn't declare as a mounting hole or
  fiducial. Some pass added a part. Find that pass; don't declare the part
  to make the error go away.
- **Collapsed pads** after the optimiser. If both pads of a two-pad passive
  sit at the same coordinate, `repair_pads` didn't run or didn't restore
  rotation.

When it finishes, snapshot before routing. Do not route in this script.

If something is wrong with the board, fix `netlist.csv` or a pass and rebuild.
Never edit the board file. The next rebuild wipes it.
