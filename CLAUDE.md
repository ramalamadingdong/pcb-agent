# Working on this board

Read this before touching anything. These rules exist because each one cost a
board or a week.

## The shape of the thing

`netlist.csv` is the source of truth. One row per pin, every pin of every IC.
The KiCad project is a **build output**. To change the circuit, edit a row and
rebuild.

```
netlist.csv  ->  build  ->  board  ->  snapshot  ->  route  ->  gerbers  ->  check
```

## Hard rules

**Never edit KiCad files by hand, and never ask the agent to.** They're
s-expressions full of coordinates. Editing them is working on geometry you
can't see, in a format where being slightly wrong looks exactly like being
right. Nothing errors out. You just get a broken board.

**Fixes go in the pipeline, never in the board file.** After routing there are
always connectivity problems the router can't sort out — isolated ground pads,
pour islands, plane fragments needing links across layers. Fix those by hand
and the next rebuild wipes all of it. Write them as a pass.

**Every pass is idempotent. Strip and rebuild, don't append.** You will re-run
a stage when you aren't sure whether it already ran. An appending pass turns
that into junk copper.

**Don't trust what a tool says about itself.** A router reporting
`"violations": []` on a pass that laid traces through other nets' pads is not
evidence. Verify with an independent check. Count segments before and after
every router call so you catch a silent no-op.

**Don't wave off a warning from a checker.** That's swapping a measurement for
an assumption.

**Don't assume generated defaults match your names.** Netclass patterns, layer
names, part rotation conventions. Nothing errors; the output is just wrong.

**Get the document.** An LLM's memory of a pin map is a guess that looks like
a fact. Pull the datasheet, cite it, and copy it into `docs/datasheets/` so an
upstream update can't quietly change your geometry later.

**Confirm the document is the document.** A fetched PDF is not verified by
its filename or its URL. One LCSC "datasheet" download turned out to be an
ISO certificate. Open it, find the part number inside it, and check one
number you already know before trusting the numbers you don't.

**Where a datasheet's text and its land drawing disagree, the drawing wins.**
It's what the factory builds.

**Recheck the numbers in your own plan.** They were written before you knew
what you know now. When one changes, write down why.

**Save the exact files you order.** Not a regenerated approximation — the
literal package, committed. Auditing an order only means something if you can
prove the zip you're reading is what shipped.

## Passes that exist because of specific tool behaviour

Don't remove these, and don't reorder them.

- **`repair_pads` runs after every optimiser call.** The placement optimiser
  writes absolute board coordinates into each footprint's local pad positions,
  collapsing both pads of a passive onto the same point. Restore rotation as
  well as position — pad angle is footprint rotation plus pad rotation, and
  restoring coordinates alone leaves every pad on a rotated part axis aligned.
- **Assert the net table is non-empty after board writes.** One KiCad
  operation rewrites the board into a stripped format that drops the top-level
  net table. KiCad reads it fine, DRC reads it fine, every fab export reads it
  fine — and the router reports "nets to route: 0" and does nothing, silently.
- **Fanout checks all four spacings it creates**, including the stub running
  past the next pad's via. DRC uses the larger of the two nets' clearances, so
  a fine-pitch escape past ground copper is held to the ground net's rule.
- **`silk_finish` repositions every refdes.** The coordinate corruption that
  hits pads also hits reference and value fields, and a pad-only repair
  doesn't touch them. The fab clips anything off board, so without this the
  boards print bare.

## Routing

Freerouting, `-mt 1`, foreground, version pinned to your Java.

Multithreaded optimisation makes clearance violations. `-mt 1` is also
usually faster, because it stops when it stops improving.

Constrain the router rather than swapping it: pre-route RF and matched pairs
as fixed copper, mark plane layers unroutable in the DSN export, let it do the
rest. Declaring an inner layer a plane costs you a routing layer — budget for
a completion pass on the remainder.

## Checking

Five levels, each catching what the one above passes:

0. Round-trip diff of the generated schematic against `netlist.csv`
1. ERC — cheap, catches little, run it anyway
2. DRC, **sampled 5+ times**. It isn't deterministic; compare which violations
   appear, not the count. Fill zones with the real filler first, or the
   refill-during-DRC invents thermal relief errors.
3. The agent reviewing the board — useful, and the one to trust least, because
   it reasons from what the build was supposed to do rather than what it did.
4. **Parse the gerbers.** `python3 scripts/validate_gerbers.py ./fab/`

Level 4 is the one that pays. Anything you find by hand becomes a rule in
`board.toml`, and the checker runs on fab output, never on the KiCad file.

## Scope

This gets you to a package a fab will build and a written reason behind every
choice in it. It doesn't replace bring-up. Done and clean and fabricated isn't
the same as working.
