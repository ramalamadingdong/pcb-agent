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

## Setup

```bash
git clone https://github.com/<you>/pcb-agent && cd pcb-agent
python3 scripts/doctor.py
```

The doctor names every missing piece and the fix for your OS. Five things have
to line up — KiCad 9, KiCad's bundled Python, a JVM matching your Freerouting,
the Freerouting jar, and `uv` — and each fails in a way that wastes an
afternoon if you don't know what you're looking at.

Then, in Claude Code:

```
/plugin marketplace add aklofas/kicad-happy
/plugin install kicad-happy@kicad-happy
```

Eleven KiCad skills by Andrew Klofas — datasheet extraction, EMC, SPICE,
sourcing from DigiKey, Mouser, LCSC and element14, fab prep for JLCPCB and
PCBWay. MIT. This is the install that matters most.

## Or: the container

The headless stages — build, route, check — can run in a container
instead, so KiCad, Java and Freerouting never need installing by hand:

```bash
make setup     # builds the pcb-agent image; also fetches the pinned
               # Freerouting jar to tools/ for native runs, checksum-verified
make doctor    # reports what the container satisfies
make check     # make targets run inside the container automatically
               # whenever the image exists — no flag to remember
```

`./run.sh <cmd>` runs any command in the container with the repo mounted at
/work, as your uid/gid, so files it writes are yours. You still install
KiCad natively to *look* at the board — the container has no GUI on
purpose, and never will (see CLAUDE.md).

Two things to know:

- **The container's KiCad major must match the KiCad you review with.** A
  KiCad 9 pcbnew cannot even load a board written by KiCad 10 — it returns
  None, no error. The base image is digest-pinned as `KICAD_BASE` in the
  Makefile; the doctor warns when host and container drift apart.
- The kicad/kicad images are linux/amd64 only; Apple Silicon runs them
  under emulation.

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

Not here yet:

- **The build passes.** `scripts/build/CONTRACT.md` specifies the interface
  each must satisfy — including the ones that exist only because of specific
  tool behaviour (`repair_pads`, the net table assertion, the four-spacing
  fanout check, `silk_finish`). `/build` and `/route` fail until they land.
- **A known-good example board**, to build first and confirm the pipeline
  works before you touch your own design.

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

MIT.
