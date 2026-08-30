---
description: Route the board from the post-build snapshot with Freerouting, then export and validate the fab package.
---

Run `make route`. This starts from the snapshot, not from a live build, so
placement can be re-run without paying for a re-route.

Non-negotiables:

- **`-mt 1`.** Multithreaded optimisation makes clearance violations. Single
  threaded is usually also faster, because it stops when it stops improving.
- **Foreground.** Backgrounded, Freerouting opens the file and exits without
  routing.
- **Version pinned to your Java.**

Verify rather than trust:

- Count copper segments before and after the router call. Correct net list
  plus zero copper is a silent no-op, and its self-report won't say so.
- A pass reporting no violations while laying traces through other nets' pads
  is not evidence. Run DRC yourself, five or more times, and compare which
  violations appear rather than the count.

Declaring an inner layer a plane costs a routing layer — expect a chunk of
nets unrouted after the first pass and finish them with a completion pass.

Then export and run `/check`. Fixes go in the pipeline, never in the board.
