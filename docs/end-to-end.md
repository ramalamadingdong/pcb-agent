# End to end: from nothing installed to a fab package

This is the whole path. An idea goes in, a package a fab will build comes
out, and an agent does the work at every stage while you review. The
method behind it is written up at
[mouro.ai/blog/pcb-design-with-ai](https://mouro.ai/blog/pcb-design-with-ai/).

Read the two paragraphs below before you start. They are the difference
between this working for you and this wasting your week.

**What is verified.** Linux and WSL, with Docker. That is where the
container path, the doctor's advice and the whole pipeline were developed
and run. **macOS is untested** — `doctor.py`'s KiCad.app python path was
written from documentation, not from a Mac. Native Windows is not a path;
use WSL. And **no board built by this pipeline has been fabricated or
powered on yet**, including the example. Passing gates is not working
hardware.

**Setup is push-button. Finishing a board is not.** The install below is
five minutes and then it works. Getting the example board from first
placement to zero clearance violations took about 19 rounds of placement
and routing, each one an edit to `board.toml` and a rebuild, and several
of them needed engineering judgment that no checker was going to supply.
Budget for that. It is the real cost, and nobody who tells you otherwise
has finished a board this way.

---

## 1. Install

```bash
git clone https://github.com/ramalamadingdong/pcb-agent && cd pcb-agent
python3 scripts/doctor.py
```

The doctor names every missing piece and the fix for your OS. Five things
have to line up — KiCad, KiCad's bundled Python, a JVM matching your
Freerouting, the Freerouting jar, and `uv` — and each one fails in a way
that costs you an afternoon if you don't recognise it.

You do not have to satisfy them by hand. The container carries all of
them:

```bash
make setup      # builds the image, fetches the pinned Freerouting jar
make doctor     # reports what the container satisfies
```

`make setup` pins the toolchain: KiCad 10 (digest-pinned base image),
Freerouting 1.9.0, checksum-verified. Once the image exists, every
headless target runs inside it automatically. `./run.sh <cmd>` runs
anything else in there with the repo mounted at `/work`, as your uid and
gid, so nothing lands in your tree owned by root.

Two things the container will not do for you. It has no GUI, so **install
KiCad natively as well** if you want to look at a board — and match the
major version to your boards, because a KiCad 9 `pcbnew` loading a KiCad
10 board returns `None` with no error at all. And `kicad/kicad` images are
linux/amd64, so Apple Silicon runs them under emulation.

## 2. Give the agent its tools

In Claude Code:

```
/plugin marketplace add aklofas/kicad-happy
/plugin install kicad-happy@kicad-happy
```

Eleven KiCad skills by Andrew Klofas — datasheet extraction, EMC, SPICE,
sourcing from DigiKey, Mouser, LCSC and element14, fab prep for JLCPCB and
PCBWay. MIT. This is the install that matters most: it is what lets the
agent read a datasheet's land drawing instead of guessing a footprint.

This repo's own five commands come from `commands/`:

```
/new-board     an idea, interviewed into a cited plan and a netlist.csv
/build         netlist.csv -> schematic -> board -> placement -> zones -> silk
/route         snapshot -> Freerouting -> DRC x5 -> gerbers
/check         validate the fab package against board.toml
/doctor        is this machine able to run any of it
```

## 3. Build the example first

Do not point this at your own design yet. Build the example, which is what
`BOARD_DIR` defaults to:

```bash
make build      # netlist -> schematic -> board -> place -> zones -> silk
make route      # snapshot -> Freerouting -> completion -> DRC x5
make export     # fab package
make check      # the gerber checker, on the real export
```

You are looking for the same gates the example signed off on: **0
unconnected, 0 clearance violations** identical across 5 consecutive DRC
runs, `validate_gerbers` **14 passed / 0 failed**, ERC clean. The 17
residual DRC items are expected — they are form-factor and
courtyard-graze artifacts, each one explained in
[`examples/unoq-power-shield/rev-1-plan.md`](../examples/unoq-power-shield/rev-1-plan.md).

If that reproduces, your toolchain is real. If it doesn't, fix that before
you spend a week on a design — you would be debugging two things at once.

## 4. Your own board

```
/new-board
```

It interviews you: what the board does, whether firmware exists, power,
interfaces, constraints, and what must not go wrong. Then it reads
datasheets and writes two artifacts into your board directory:

- `rev-1-plan.md` — scope, requirements each cited to a document, part
  choices with the reason, capacity math with the arithmetic shown, and
  the gate list that has to pass before anyone orders
- `netlist.csv` — one row per pin, every pin of every IC

**It stops twice for your sign-off**, once after the interview and once
before it writes the netlist. Those stops are the safety story. An
idea-to-board prompt with no stops produces sixty confident rows of
hallucinated pin numbers, and you find out when the boards arrive.

The rule that governs the whole stage: no pin number that didn't come from
a document read in-session and cited. The round-trip diff holds the
schematic to the CSV. Nothing holds the CSV to reality except that rule.

Then write `board.toml` — start from `templates/board.example.toml`, which
is commented section by section, and read
[`your-own-board.md`](your-own-board.md) for what each section drives. One
file configures both the build passes and the checker, in one coordinate
frame with the origin at the board's bottom-left corner.

## 5. The placement loop

```bash
make build BOARD_DIR=path/to/yours
./run.sh kct placement check path/to/yours/your-board.kicad_pcb
```

Placement is a loop, not a step. Fix the **errors** by editing
`[[floorplan.place]]` coordinates and rebuilding; the pass is idempotent,
so re-runs are cheap. The example went 26 → 10 → 6 → warnings-only over
four iterations. That is normal, not failure.

Two things that save rounds here, both learned the expensive way:

- **Place passives by net topology, not by schematic tidiness.** A part
  whose pins exit south, sitting north of its chip, gives the router no
  short path that doesn't graze pads. Rotation is placement too — a part
  in the right spot facing the wrong way is a part in the wrong spot.
- **A bounded copper pour is a wall.** Exported to the DSN it is fixed
  copper, and the router will not cut through it. If the router keeps
  making the same bad choice in the same corner, check whether a pour
  closed the corridor you assumed it would use. Shrink the pour rather
  than fighting the router.

## 6. Route and check

```bash
make route BOARD_DIR=path/to/yours
make export BOARD_DIR=path/to/yours
make check BOARD_DIR=path/to/yours
```

Read the route JSON, not the router's mood. `segments_after_router` must
be well above `segments_before` — a silent no-op is detected and fails,
but look anyway. `unfixed` names the nets completion couldn't finish;
those usually mean a congested corner, so give the parts room in the
floorplan rather than fighting the router. The DRC block shows violation
*kinds* across five runs: stable kinds are real, unstable ones are DRC
nondeterminism.

`make check` runs the level-4 checker on the actual export. Rules you
don't configure report `SKIP`, not `PASS` — investigate SKIPs, don't count
them. A check you haven't written isn't a check that passed.

## 7. Gates before you order

Nothing ships until all of these hold:

- schematic round-trip diff clean (the build fails otherwise)
- ERC clean, DRC ×5 with zero errors and every warning explained
- `make check` green, SKIPs investigated
- every part re-verified in stock **at order time**, not at design time
- the exact ordered files committed byte-for-byte

Anything you catch by hand on the way becomes a line in `board.toml`, so
the checker catches it for you next time. That is the whole point of the
config file.

## What this does not do

It gets you to a package a fab will build and a written reason behind
every choice in it, with respins in hours instead of days because the
fixes are in code.

It does not replace bring-up. Done and clean and fabricated isn't the same
as working — and as of today, nothing built this way has been fabricated
at all.
