# Reviewing a board in KiCad

People review the schematic and open the board in KiCad. What they open
has to be the board that ships. A rebuild that looks the same isn't good
enough. This page covers how the pipeline guarantees that, and what a
reviewer does.

## The problem

The schematic is drawn from `netlist.csv` before placement. The board is
changed a lot after that: placement, fanout, routing, and the post-route
passes (`post_route_fix`, `finish_routes`, `cleanup_pass`), which add and
delete copper to fix what the router left behind. Nothing ever redraws
the schematic afterwards. A pass that moves a pad onto another net, adds
a footprint, or lays copper across two nets would leave a schematic that
still looks right.

There was also a quieter gap. `kct create-pcb` wrote footprints with no
link to their symbols. Even when the two files matched, KiCad saw two
unrelated documents: 61 parity errors on the example, no cross-probing,
and F8 (Update PCB from Schematic) proposing to delete every placed part.

## How it's closed

**The schematic is never updated from the board.** If a post-route fix
needs to change the circuit, the change goes in `netlist.csv` (or
`[mounting_holes]` / `[fiducials]` in `board.toml`) and both documents
are rebuilt. The passes below make that rule enforceable.

| Step | What it does |
|---|---|
| `link_schematic` (end of `make build`) | Writes each footprint's symbol path, sheet, library id and DNP flag from the schematic, plus `fp-lib-table` / `sym-lib-table`. Marks the holes and fiducials `board.toml` declares as "Not in schematic". Fails on any other footprint with no symbol. It only links: it never makes the board agree with the schematic. |
| `check_parity` (`make parity`, and inside `make export`) | Runs on the **final** board, after every post-route pass. Uses four independent checks, listed below. Export refuses to plot if it fails. |
| `validate_gerbers --kicad-netlist` (`make check`) | Reads the X2 pad attributes (`%TO.P`, `%TO.N`) in the plotted copper and checks them against eeschema's netlist export. These are the bytes the fab receives. |
| `release` (`make release`) | Freezes the project, the evidence, and the fab package into `release/rev-N/` with `MANIFEST.sha256`. Refuses unless parity and the gerber check pass on those bytes. Immutable. |
| `verify_release` (`make verify-release`) | Checks hashes and the zip. In the container it also re-exports from the released board and requires the same package. |

The four checks in `check_parity`:

1. **KiCad's own parity** (`kicad-cli pcb drc --schematic-parity`) must
   report zero items. This is what the reviewer's KiCad would say.
2. **An independent diff:** eeschema's netlist export against every pad
   pcbnew reads off the board. It compares parts, values, footprints, DNP
   flags, sheets, symbol links and the net of every pad. A board-only
   footprint must be declared in `board.toml` and carry no net, so "Not in
   schematic" can't be used to hide a circuit change.
3. **Copper matches the pad nets.** DRC runs 5 times, and any
   `shorting_items`, `tracks_crossing` or unconnected item in any run
   fails. Pad nets are only labels; this is the geometric check.
4. **Gerber pad nets**, done in `validate_gerbers` as above.

`scripts/build/test_parity.py` damages a linked copy of the board in 8
ways a pass could, including re-netting a pad, adding a stray part,
laying a shorting track, hiding a part as board-only, and dropping a
part. It requires the gate to fail every time.

## For the reviewer

```bash
make verify-release BOARD_DIR=...          # before you open anything
```

1. Open `release/rev-N/<board>.kicad_pro`. Nothing else in the repo is
   the reviewed design. `release.json` records the commit and KiCad
   version it came from.
2. Review, cross-probe, run ERC/DRC as you like. **Don't save.** Put your
   findings in comments, an issue, or the rev plan.
3. Run `make verify-release` again when you're done. KiCad leaves
   `.kicad_prl`, `fp-info-cache` and backup files behind; those are
   ignored. A saved schematic or board changes a hash, and the verifier
   names the file.

A requested change goes into `netlist.csv` / `board.toml`, then `make
build route export check`. Bump `[build] revision` and run `make
release`, which produces `rev-N+1`. The old release stays as it was
ordered.

## Limits

- Flat schematics only. That's what `generate_schematic` writes; the
  parity passes fail on hierarchical sheets rather than guess.
- Re-derive needs the KiCad major the release was made with. A different
  KiCad can plot different bytes. The verifier then names the files that
  differ, and `release.json` says which version made the originals.
- DRC `lib_footprint_mismatch` warnings (a placed footprint differs from
  its library copy) are visible now that footprints name their library.
  They are expected where a pass sets per-pad overrides. They're reported,
  not gated.
