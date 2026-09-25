---
description: Turn a plain-language board idea into a cited requirements doc and a netlist.csv the build pipeline can compile. Use at the start of any new board or major revision.
---

# /new-board

The user has an idea for a circuit board. Your job is to turn it into two
artifacts the pipeline can build from:

- `docs/rev-<N>-plan.md` — scope, requirements, part choices, capacity math,
  and the list of checks that must pass before anyone orders
- `netlist.csv` — one row per pin, every pin of every IC

You do **not** touch KiCad. You do not run the build. Those come after, and
only once a human has signed off on the plan doc.

## The rule that governs everything here

An LLM's memory of a pin map is a guess that looks like a fact.

Every pin number, every bus address, every electrical limit you write down
must come from a document you actually read in this session — a datasheet
PDF, a vendor reference design, a distributor page — and must carry a
citation. If you cannot cite it, you do not know it. Say so and stop.

Copy every datasheet you use into `docs/datasheets/` so an upstream revision
can't silently change your geometry later.

---

## Phase 0 — Interview

Do not start designing. Ask, and wait for answers. The user has an idea in
their head; your first job is to get it out intact.

Ask about, in roughly this order:

1. **What does it do?** One paragraph, in their words.
2. **Is there firmware?** Existing repo, or written later? If it exists, get
   the path — it's the best requirements source you have.
3. **Power.** Battery or wall? Chemistry and capacity? USB-C charging? What's
   the peak current draw and from what?
4. **Interfaces.** What talks to what, and over what bus. What has to be
   exposed to the outside world — connectors, buttons, LEDs, antenna.
5. **Constraints.** Board size or an enclosure it must fit. Layer count.
   Assembly house and process. Budget per board and quantity.
6. **What must not go wrong.** The thing that would make them throw the
   batch away.

Ask what you need and no more. If the user gives you a detailed spec up
front, skip to Phase 1 and confirm your reading of it in one paragraph.

**Stop here and show the user your understanding before continuing.**

---

## Phase 1 — Requirements, cited

If firmware exists, read it before anything else. Write down what the
hardware has to provide, every line citing a file and line number:

- Bus addresses and which peripheral sits at each
- Clock rates, sample rates, baud rates
- Partition sizes and filesystem layout
- Which peripherals are behind compile-time flags, and are therefore optional

That last one is what makes a new board cheap. A headless code path means a
board with no display needs no new firmware, just a pin map.

If there is no firmware yet, derive the same list from the user's answers and
mark each line `ASSUMED` so it's obvious what hasn't been verified.

---

## Phase 2 — Architecture and parts

Propose a block diagram in prose first — what parts, what connects to what,
why. Get agreement before sourcing.

Then source. Use the `digikey`, `mouser`, `lcsc` and `element14` skills, and
check more than one; they don't agree.

For every significant part record:

- Part number, price, **stock and lead time**
- Why this part — tied to a specific requirement from Phase 1
- What you rejected and why
- The datasheet, downloaded to `docs/datasheets/`

Lead time is a real constraint, not a footnote. A part with more stock and a
22-week lead is worse than one with less stock available now. When a cheaper
part can serve, state exactly which capabilities the firmware actually uses
and confirm the replacement provides all of them — a swap that deletes parts,
a crystal, and a layout constraint is worth more than the unit price
difference.

---

## Phase 3 — The pass that earns its keep

A BOM with the right ICs on it is usually still missing everything that makes
them run. Read every datasheet against the proposed connection table and
check specifically for:

- **Charger load-share path.** Hang the system load off the battery pin and
  the charger reads load current as charge current, never terminates, and
  trickles the cell forever.
- **Battery sense divider**, if firmware reports a battery percentage.
- **RF front end and antenna connector** on any receiver.
- **Strap and mode pins** tied somewhere. Floating, the part comes up in the
  wrong interface mode and never answers.
- **Decoupling** per the datasheet, not per habit.
- **Pull-ups** on every open-drain bus, with the value calculated.

Then do the capacity math and write it down:

- Data rate against storage size → how long does it record before it's full
- Current against trace width (IPC-2221, state the copper weight)
- Dissipation against package thermal resistance
- Battery capacity against average draw → runtime

If any of these comes out badly, that's a design change, and it's cheaper now
than after the boards arrive.

**Stop here. Show the user the plan doc and get sign-off before writing the
netlist.**

---

## Phase 4 — netlist.csv

The schematic is not the source of truth. This file is. The KiCad project is
a build output.

```
Net,RefDes,Pin,PinName,Direct,Note
VBUS,J1,A4,VBUS,,USB-C receptacle
VBUS,U4,4,VDD,,charger input
VBUS,D1,A,Anode,,Schottky into VSYS
VBUS,C7,1,~,yes,"100 nF, pad touching a VBUS pin"
GND,J1,SH1,SHIELD,,"chassis, stitch to GND with 4 vias"
```

Rules:

- **Every pin of every IC gets a row.** No exceptions.
- **`NC` written out** where a pin is meant to be unconnected, with a note
  saying why. A missing row and an intentional no-connect must not look the
  same.
- **`Direct` is optional** and usually empty. Tag ONE pin of a part with
  `yes` and `direct_connect.py` finds the other pins already on that net and
  places the part with that pad touching one of them — no trace. `REF` or
  `REF.PIN` limits the search to that part or pin. Use it for decoupling
  caps on IC supply pins and TVS parts on connector pins. It can't be
  combined with `[[floorplan.place]]` for the same part, and a tagged part
  can't be another tag's target. The schematic draws the part hanging off a
  short wire from that same pin, so the review shows what the board does.
- **The note column carries the reason**, not a restatement of the pin name.
  Cite the datasheet page where the choice came from a document.
- Pin numbers come from the datasheet's land drawing, not its text. Where the
  two disagree, the drawing is what the factory builds — a chip antenna whose
  text says two terminals and whose drawing shows three pads ships dead if
  you trust the text.

When the file is written, hand off:

> `netlist.csv` is ready. Run the build to generate the schematic. The
> round-trip check will fail the build if any net in the CSV didn't make it
> into the drawing.

---

## Phase 5 — The gate list

Close the plan doc with the checks that must pass before anyone orders. This
is the most important list in the document. At minimum:

- [ ] Schematic round-trip diff clean against `netlist.csv`
- [ ] ERC clean
- [ ] DRC run 5+ times, comparing which violations appear rather than the
      count — DRC isn't deterministic
- [ ] `validate_gerbers` PASS on the exported package
- [ ] Every part in stock at order time, re-verified
- [ ] The exact ordered files saved byte-for-byte

Add board-specific gates from Phase 3. Anything you found by hand becomes a
line in `board.toml` so the checker finds it next time.

---

## What you must not do

- **Don't edit KiCad files.** They're s-expressions full of coordinates.
  Being slightly wrong looks exactly like being right, nothing errors, and
  you get a broken board. Change a CSV row and rebuild.
- **Don't fix anything in the board file.** Fixes go in the pipeline. The
  next rebuild wipes anything done by hand.
- **Don't assume generated defaults match your names.** Netclass patterns,
  layer names, rotation conventions. Nothing errors; the output is just
  wrong.
- **Don't wave off a warning.** That's swapping a measurement for an
  assumption.
- **Don't skip a stop point** because the user seems in a hurry.

## What this does not give you

A plan and a netlist, not a working board. Done and clean and fabricated
isn't the same as working. This gets you to a package a fab will build and a
written reason behind every choice in it. Bring-up is still bring-up.
