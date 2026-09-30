# Your own board, start to fab package

The example (`examples/unoq-power-shield/`) is the reference
implementation — every section named here exists there, filled in with
cited values. Build it once before trusting any of this with your own
design. This page is the map; the example is the territory.

## 0. Preflight

```bash
python3 scripts/doctor.py     # names every missing piece and the fix
make setup                    # or install natively per the doctor
```

## 1. Plan and netlist — `/new-board`

Run the `/new-board` command (in Claude Code) or follow
`commands/new-board.md` by hand. It ends with two artifacts in your board
directory: a plan doc with every choice cited, and `netlist.csv` — one row
per pin, every pin of every IC. The rule that governs it: **no pin number
that didn't come from a document read in-session and cited.** The
schematic generator's round-trip diff will hold you to the CSV; nothing
holds the CSV to reality except that rule.

## 2. The build config — board.toml, section by section

One file drives both the build passes and the gerber checker. **One
coordinate frame everywhere: origin at the board's bottom-left corner,
X right, Y up** — the frame of a drill file exported with the aux origin
at that corner. `export_fab.py` pins the aux origin there, so your
config's rectangles and the checker's gerber coordinates are the same
numbers. Never write a KiCad page coordinate into board.toml.

| Section | Consumed by | What it is |
| --- | --- | --- |
| `[board]`, `[frame]` | checker + passes | name; the outline bbox every coordinate rule is anchored to |
| `[build]` | create_pcb | width/height/layers/title/revision |
| `[libs]`, `[[libs.symbols]]` | make_libs, generate_schematic | project library name, vendored-footprint dir, and pin tables for ICs with no stock symbol — pin numbers from the datasheet's land drawing |
| `[parts.<REF>]`, `[[part_rules]]` | make_libs, generate_schematic | symbol + footprint per refdes (rules by prefix for jellybeans, per-ref overrides for everything else) |
| `[[floorplan.place]]` | floorplan, place | hand placements, locked; see the loop below |
| `[direct]` | direct_connect | `overlap_mm` / `clearance_mm` for `Direct`-tagged netlist rows: parts placed pad-on-pad against a pin already on their net. Every pair shows up in DRC as an expected `courtyards_overlap`, listed in the pass's JSON; the DRC gate accepts exactly those pairs (tagged part against one of its own netlist targets) without a ledger entry |
| `[mounting_holes]`, `[fiducials]` | passes AND checker | positions place them; count/diameter checks them in the gerbers |
| `[[keepouts]]` | add_keepouts, silk, router passes, both checkers | one rectangle feeds the rule area, the silk dodge, the router fences, the board check and the gerber check |
| `[[connectors]]`, `[connector_footprints]`, `[connector_policy]` | check_placement | every part a person plugs into. Edge connectors: the room the plug and cable need, **and which way the part actually faces**, derived from its rotation against the footprint's datasheet opening. Vertical/stacking headers: a clear access ring on the mating side. An undeclared J/P/CN/USB ref fails |
| `[drc]`, `[[drc.accept]]`, `[[erc.accept]]` | drc_sample, erc_check | the ledger of explained violations: exact refs plus a reason. Anything else fails route and check, and so does an entry that stops matching |
| `[[zones.pour]]`, `[layers] planes` | zones, export_dsn, checker | pours; plane layers are routing-banned and gerber-checked for stray tracks |
| `[nets.power]` | apply_netclasses + checker | patterns pin nets to the power netclass BY NAME; `min_width_mm` is the checker floor; `plane_fed` exempts short pad-escape chains on plane-backed nets |
| `[[fanout.thermal_vias]]` | fanout | vias at named pads — exposed pads, and any pour island the DRC shows floating |
| `[route]`, `[route.completion]` | route chain | widths, via, clearance. **Set completion `clearance_mm` equal to your DRC minimum** or every completion link lands as a violation |
| `[fab]` | export_fab | output naming; optional JLC BOM/CPL generation |

Three traps that cost this repo real debugging time, so you don't pay
them again:

- **Footprints come from land drawings, not text**, and a vendored
  `.kicad_mod` must be strict KiCad syntax — `(layer …)` goes *outside*
  `(stroke …)`. The lenient parser in the toolchain will accept the wrong
  nesting and pcbnew will reject the whole board with an error pointing
  at an unrelated line.
- **Pin-header strips run downward from pin 1** at rotation 0 (KiCad Y
  runs down, our frame runs up). Check where pin N lands before locking a
  connector next to something else. The example shorted an LED to a
  header pin this way.
- **Netclass clearance must equal your DRC minimum.** A smaller netclass
  clearance reaches the DSN, freerouting routes to it, and every squeeze
  becomes a DRC violation you then chase one at a time.

## 3. The build loop

```bash
make build BOARD_DIR=path/to/yours     # libs → schematic → board → place → zones → marks → silk
```

Placement is a loop, not a step. After every build:

```bash
./run.sh kct placement check path/to/yours/your-board.kicad_pcb
```

Fix the **errors** by editing `[[floorplan.place]]` coordinates and
rebuilding — the pass is idempotent, re-runs are cheap. The example went
26 → 10 → 6 → warnings-only over four iterations; that's normal, not
failure. Warning-class items (edge-connector overhang, screw-head
courtyard grazes) are judgment calls the report describes precisely.

## 4. Route and check

```bash
make route BOARD_DIR=path/to/yours     # snapshot → freerouting → completion → DRC ×5, gated
make export BOARD_DIR=path/to/yours    # gerbers, drill, pos, IPC-D-356
make check BOARD_DIR=path/to/yours     # ERC, pad angles, DRC ×5, placement, fab package
make render BOARD_DIR=path/to/yours    # review/*.png: look at it
```

Read the route JSON line, not the router's mood:
- `segments_after_router` must be well above `segments_before`. A silent
  no-op is detected and fails, but look anyway.
- `unrouted_after_router` is what Freerouting left, counted by pcbnew
  before completion.
- `unfixed` names nets the completion couldn't finish.
- The `drc` block lists every violation not in your `[[drc.accept]]` ledger.
  The route fails if there is any.

The first route of a new board will fail the DRC gate. That is expected.
For each violation, either fix it (almost always in the floorplan) or,
when it is a real exception, add a `[[drc.accept]]` entry naming its refs
and the reason a reviewer would accept.

Nets the completion leaves unfixed usually mean a congested corner:
give the parts room in the floorplan rather than fighting the router.
`/improve-placement` does that as measured trials. It looks at the render,
proposes floorplan edits, rebuilds and re-routes each one in a scratch
copy, and keeps only what `score_route.py` measures as better. Then it
stops for your sign-off.
A DRC "zones not connected" usually means a pour island holding a pad
with no via — `island_diag`-style inspection names the pad; give it a
`[[fanout.thermal_vias]]` entry.

## 5. Gates

Your plan doc ends with the gate list. Nothing ships until:

- schematic round-trip diff clean (the build fails otherwise)
- ERC clean, DRC ×5 with zero errors and explained warnings: `make check`
  enforces both, against the ledger in board.toml
- `make check` green — SKIPs investigated, not counted
- every ledger entry's reason re-read. An acceptance is a claim.
- every part re-verified in stock at order time
- the exact ordered files committed byte-for-byte

Anything you catch by hand on the way becomes a line in board.toml, so
the checker catches it next time. That is the whole point.
