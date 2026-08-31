# pcb-agent

Design a circuit board end to end with an agent. Parts, netlist, placement,
routing, gerbers a fab will build. You review, it does the work.

The method: **the design lives in a CSV, not in KiCad.** One row per pin.
Scripts build the schematic and the board from it. Nothing in the KiCad files
is edited by hand — to change the circuit you edit a row and rebuild. Then
five levels of checking, the last of which parses the exported gerbers,
because the gerbers are what the factory builds.

Written up in full at
[mouro.ai/blog/pcb-design-with-ai](https://mouro.ai/blog/pcb-design-with-ai/).

## Status: passes landed, example board unproven

The build passes are in `scripts/build/` and the whole pipeline runs end to
end against the example board: netlist → schematic (clean round-trip diff) →
board → netclasses → floorplan → holes → zones/fanout → freerouting →
completion → fab export → checker. As of 2026-08-30 the example has passed
its pre-order gates — **0 unconnected, 0 clearance violations** across 5
identical DRC runs, `validate_gerbers` 14/0, ERC clean, every residual DRC
item explained in its `rev-1-plan.md` ledger — but **no physical board has
been fabricated yet**. Until one comes back and powers on, treat
`examples/unoq-power-shield` as a pipeline exercise, not a verified
reference design.

If you want only the fab-output checker, it stands alone with no dependencies
at [gerber-check](https://github.com/ramalamadingdong/gerber-check).

Platform honesty: developed and verified on Linux (WSL) — the container
path, the clean-box doctor advice, and the whole pipeline. **macOS is
untested**: `doctor.py`'s KiCad.app python path was written from
documentation, not a Mac, and is the most likely thing to be wrong. If you
hit it, an issue with your `doctor.py` output is gold.

## Setup

```bash
git clone https://github.com/ramalamadingdong/pcb-agent && cd pcb-agent
python3 scripts/doctor.py
```

The doctor names every missing piece and the fix for your OS. Five things have
to line up — KiCad (9 or 10, **matching the major your boards use**: a KiCad 9
pcbnew returns None loading a KiCad 10 board, no error), KiCad's bundled
Python, a JVM matching your Freerouting, the Freerouting jar, and `uv` — and
each fails in a way that wastes an afternoon if you don't know what you're
looking at.

### Or: the container

The headless stages run in a container instead, so KiCad, Java and
Freerouting never need installing by hand:

```bash
make setup     # builds the pcb-agent image; fetches the pinned Freerouting
               # jar to tools/ for native runs, checksum-verified
make doctor    # reports what the container satisfies
make build route export check   # run in-container automatically when the
                                # image exists (BOARD_DIR picks the board)
```

`./run.sh <cmd>` runs any command in the container with the repo mounted at
/work, as your uid/gid. You still install KiCad natively to *look* at the
board — the container has no GUI (the one X11 artifact inside is xvfb, a
virtual framebuffer that exists solely because Freerouting 1.9.0 asks for a
screen size it never uses). The base image is digest-pinned (`KICAD_BASE` in
the Makefile) and must match your native KiCad major; the doctor warns on
drift. kicad/kicad images are linux/amd64 — Apple Silicon runs them under
emulation.

Then, in Claude Code:

```
/plugin marketplace add aklofas/kicad-happy
/plugin install kicad-happy@kicad-happy
```

Eleven KiCad skills by Andrew Klofas — datasheet extraction, EMC, SPICE,
sourcing from DigiKey, Mouser, LCSC and element14, fab prep for JLCPCB and
PCBWay. MIT. This is the install that matters most.

## Use

```
/new-board     an idea, interviewed into a cited plan and a netlist.csv
/build         netlist.csv -> schematic -> board -> placement -> zones -> silk
/route         snapshot -> Freerouting -> DRC x5 -> gerbers
/check         validate the fab package against board.toml
/doctor        is this machine able to run any of it
```

`/new-board` stops twice for your sign-off: once after the interview, once
before it writes the netlist. Those stops are the safety story. An
idea-to-board prompt with no stops will produce sixty confident rows of
hallucinated pin numbers and you won't find out until the boards arrive.

From netlist to fab package for your own design:
[docs/your-own-board.md](docs/your-own-board.md) — the board.toml sections,
the placement loop, and the traps this repo already paid for.

## What's here and what isn't

Working now:

- **`scripts/validate_gerbers.py`** — the level-4 checker. Reads exported
  gerbers and drill files as text. No dependencies, no install, doesn't need
  KiCad. Runs on any fab package from any tool. See below.
- **`scripts/doctor.py`** — environment preflight.
- **`CLAUDE.md`** — the rules. This is what keeps an agent from undoing the
  work: don't edit board files, fixes go in the pipeline, every pass
  idempotent, don't trust a tool's self-report, get the document.
- **`commands/`** — the five slash commands.
- **`templates/`** — `netlist.csv` schema and a commented `board.toml`.

Also here now:

- **`scripts/build/`** — the build passes, ported from the pipeline that
  shipped real boards. They encode specific tool behaviour discovered by
  getting boards back broken — `repair_pads` restoring rotation as well as
  position, the net-table assertion after every board write, the
  four-spacing fanout check, `silk_finish` repositioning every refdes —
  with the comments explaining why each guard exists.

Not here yet:

- **An example board that has been powered on.** `examples/unoq-power-shield`
  has a cited plan and netlist, but until one is built and brought up it is a
  reference design nobody has verified.

## The gerber checker on its own

Useful even if you never adopt the rest:

```bash
python3 scripts/validate_gerbers.py --init      # writes board.toml
python3 scripts/validate_gerbers.py ./fab/
```

Catches what passes ERC, passes DRC, passes every fab export, and still ships
a broken board:

| Check | Why nothing else sees it |
| --- | --- |
| Signal traces on a layer you declared a plane | DRC doesn't know it's a plane. Breaks the reference for anything routed over it. |
| Power nets at default trace width | Generated netclass patterns (`VCC*`, `VDD*`, `+*V`) match none of the usual net names. Nothing errors, the traces just come out thin. |
| Missing mounting holes | Not netlist objects. Nothing emits them unless you write the pass. |
| Silkscreen past the board outline | The fab clips it. Boards print bare. |
| Fiducials missing, collinear, or in a keepout | Keepout rules ban pours, tracks and vias — not pads. |
| Too few thermal vias under an exposed pad | No rule covers "enough". |
| RF trace over budget | Nothing measures it. |

Rules you don't configure report `SKIP`, not `PASS`. A check you haven't
written isn't a check that passed.

Export with X2 attributes on — net names and fiducial tags ride in those, and
without them the net-aware checks go blind.

## Scope

This gets you to a package a fab will build and a written reason behind every
choice in it. Respins in hours instead of days, because the fixes are in code.

It doesn't replace bring-up. Done and clean and fabricated isn't the same as
working.

## Credit

- [kicad-happy](https://github.com/aklofas/kicad-happy) — Andrew Klofas, MIT
- [kicad-tools](https://github.com/rjwalters/kicad-tools) — RJ Walters, MIT
- [Freerouting](https://github.com/freerouting/freerouting)
- [KiCad](https://www.kicad.org/)

GPL-3.0 — the build passes import `pcbnew`, which is GPL-3.0, and now that
they've landed the repo follows.
[gerber-check](https://github.com/ramalamadingdong/gerber-check) is pure
stdlib text parsing, links nothing, and stays MIT.
