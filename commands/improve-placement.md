---
description: Look at where routing got stuck, propose floorplan edits, and keep only the ones a rebuild and re-route prove better. Stops for sign-off before touching the real board.toml.
---

The picture proposes and the measurement decides. You look at renders of
the board and propose changes to its **floorplan**, meaning the
`[[floorplan.place]]` x / y / rotation / side and the `[floorplan] anchors`
in `board.toml`. The pipeline rebuilds and re-routes each proposal, and
`score_route.py` judges it. You never draw copper and never edit a KiCad
file. A proposal that scores worse is logged and dropped, however good it
looked.

Budget: **3 trials** unless the user gives another number. Each one is a
full build plus route, about 15 minutes, most of it the placement loop.

## 0. Baseline

The board must already be built and routed (`make build route`), so that
`$(BOARD_DIR)/build/route.json` exists.

```bash
make render      # review/<board>-all.png, one PNG per copper layer, review/kicad/
make score       # build/score.json -- the number to beat
cp $BOARD_DIR/build/score.json $BOARD_DIR/build/score-baseline.json
```

Read every PNG in `review/` with the Read tool: the whole board first
(`-all`), then each layer. What the colours mean:

- **red lines:** nets still in pieces, with the net name on the line;
- **orange copper:** nets Freerouting left that the completion passes
  (finish_routes) finished. This is where the router struggled;
- **magenta:** segments not at 45 degrees, which the completion passes drew.

Also read `route.json`'s `unrouted_after_router`. That is pcbnew's count of
what Freerouting left, taken before completion, and it is the number that
tells you where the router struggled.

## 1. Propose, at most 3 edits per trial

For each edit, write down the reason in terms of the picture: "the 3V3_UNO
ratsnest crosses the whole board from J11 to Q1-Q4, so move the level
shifters toward J11", or "R3/R4 sit in U1's escape path, and 6 orange nets
detour around them". Good edits shorten the red and orange runs, uncross
them, or open a channel. Only move parts the floorplan already places, or
add a part to `[[floorplan.place]]` with a reason.

**Never propose a change that `check_placement` would fail.** Connectors
keep a face that reaches their edge and a rotation that points their
opening at it (the check derives the facing from the rotation, see
`[connector_footprints]`). Keepouts stay clear. The score ranks any
placement failure below every routing result, so a "better-routing"
trial that rotates a connector inward loses automatically. Don't spend
a trial on one.

## 2. Run the trial

Never edit the real `board.toml` during a trial. Copy the board's SOURCE
files (not its build outputs) into a trial directory and edit the copy:

```bash
T=$BOARD_DIR/build/trials/t1            # build/ is gitignored and cleaned
mkdir -p $T
cp $BOARD_DIR/board.toml $BOARD_DIR/netlist.csv $T/
cp -r $BOARD_DIR/<[libs] vendor_dir> $T/      # e.g. footprints.pretty
cp $BOARD_DIR/bom.csv $T/ 2>/dev/null || true
# edit $T/board.toml: only the floorplan entries you proposed
make build route render score BOARD_DIR=$T NAME=<board name> \
     BASELINE=$BOARD_DIR/build/score-baseline.json
```

`NAME` pins the file names to the real board's. Without it they would be
named after the trial directory.

The score line prints `better`, `worse` or `same`, and which component decided
it (placement, DRC, router_left, unfinished, vias, length, in that order).

## 3. Log every trial, kept or not

Append to `$BOARD_DIR/placement-trials.md`, which is committed and is the
record:

```markdown
## Trial N: YYYY-MM-DD
Edits:  J3 (3.6, 46.5, 270) -> (3.6, 40.0, 270); anchors += ["U2"]
Why:    <what you saw in which render>
Before: placement 0, drc 0, router_left 41, unfinished 0, vias 114, 1605.8 mm
After:  placement 0, drc 0, router_left 29, unfinished 0, vias 109, 1571.2 mm
Result: better (router_left 41 -> 29). Kept as the new baseline.
```

A worse trial is logged with the component that decided it and then
discarded. Its trial directory can be deleted. If a trial is better, its
score becomes the baseline for the next trial, and later trials build on
its `board.toml`.

## 4. Stop for sign-off

When the budget is spent, show the user:
- the diff between the real `board.toml` and the best trial's `board.toml`;
- the before and after score, and the log;
- the best trial's `-all.png` next to the baseline's.

**Do not write the winning edits into the real `board.toml` without an
explicit yes.** After the yes: apply exactly that diff, then run `make build
route export check` on the real board. The trial result was a measurement of
the trial, and the real board has to earn its own.

## What this is not

- It is not a router. It never places copper, and a picture is not geometry.
  Coordinates you propose come from `board.toml`'s frame (bottom-left
  origin, Y up), not from pixel positions in the render.
- It does not edit `netlist.csv`. Swapping equivalent pins is a netlist
  change with firmware consequences, and it is out of scope here.
- A trial that is better on one routing run may be luck, since Freerouting
  isn't deterministic. When a trial wins by a small margin (router_left
  within about 10 %), re-run `make route score` on it once before calling
  it better.
