---
description: Check that this machine can run the pipeline — KiCad 9, pcbnew, Java, Freerouting, uv. Run this first, and any time a build fails in a way that smells like environment.
---

Run `python3 scripts/doctor.py` and report the result.

If anything is MISSING, walk the user through the fixes it prints, one at a
time, in the order shown. Do not start a build until it exits clean —
a missing `pcbnew` or a backgrounded Freerouting fails silently, and you will
spend the afternoon debugging the board instead of the setup.
