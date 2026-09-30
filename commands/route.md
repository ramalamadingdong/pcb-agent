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
  is not evidence. `route.py` runs DRC five times (`drc_sample.py`), and
  **fails** on anything unexplained in any run: an error missing from the
  `[[drc.accept]]` ledger, an unconnected item, or a ledger entry that no
  longer matches. Fix the board. Add a ledger entry only for a real,
  explained exception, with its reason.
- `unrouted_after_router` in the JSON is what Freerouting left, counted by
  pcbnew before the completion passes. When it is large, the fix is usually
  placement: see `/improve-placement`.

Declaring an inner layer a plane costs a routing layer — expect a chunk of
nets unrouted after the first pass and finish them with a completion pass.

Then export and run `/check`. Fixes go in the pipeline, never in the board.
