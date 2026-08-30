---
description: Validate an exported fab package against board.toml. Run after every export, and always before ordering.
---

Run the gerber checker on the fab package:

```bash
python3 scripts/validate_gerbers.py ./fab/ -c board.toml
```

Report every line. Then:

- **Any FAIL** — fix it in the pipeline, never in the board file. Re-run the
  build, re-export, re-check. A fix made by hand is wiped by the next rebuild.
- **Any SKIP** — that is a check nobody wrote. Say so plainly. A rule you
  never configured is not a rule that passed.
- **All PASS** — good, and still not evidence the board works. Done and clean
  and fabricated isn't the same as working.

If the user found something by hand that the checker missed, add it to
`board.toml` now, while you know what it was. That is how this file earns its
keep.
