# KiCad 11 and the end of the SWIG bindings

*Findings as of 2026-08-30, against the KiCad developer docs. No code here —
this is the record of what's coming and what our options are.*

## The claim, confirmed

The [PCB Python bindings page](https://dev-docs.kicad.org/en/apis-and-binding/pcbnew/index.html)
of the KiCad developer docs says the SWIG-based `pcbnew` bindings are
**deprecated as of KiCad 9.0**, and, quoting exactly:

> The current plan is to remove the SWIG bindings in KiCad 11.0.

So: present in 9 and 10 (verified by import in both `kicad/kicad:9.0` →
9.0.9 and `kicad/kicad:10.0` → 10.0.5 container images), gone in 11.

## What replaces them, and why it doesn't serve us

The replacement is the [IPC API](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html):

> The IPC API is an interface that can be used to remotely control a
> **running instance of KiCad**.

It is a client/server protocol — each running KiCad instance acts as the
server, plugins connect as clients. The Pythonic client is
[kicad-python](https://gitlab.com/kicad/code/kicad-python). Two properties
matter for this pipeline:

1. **It operates on a live KiCad process, not on files.** The SWIG bindings
   load a `.kicad_pcb` standalone; the IPC API needs the PCB Editor running.
   The [known headless workaround](https://adamws.github.io/using-the-new-kicad-ipc-api-in-a-ci-environment/)
   is launching the full `pcbnew` GUI under Xvfb with pre-seeded config to
   suppress first-run dialogs — that is, not headless at all, just invisible.
   True headless operation is *planned* upstream, not shipped.
2. **In KiCad 9/10 the IPC API cannot plot or export files at all** — that
   support lands in KiCad 11+.

Our container is deliberately GUI-free (no X11, no VNC — the CLAUDE.md
rule). A headless build container is exactly the environment the IPC API,
as it stands, does not serve.

`kicad-cli` is unaffected by any of this — it is a native binary, and it
keeps doing what it does (ERC, DRC, gerber/drill export, rendering).

## What in this pipeline actually depends on SWIG

Per `scripts/build/CONTRACT.md`, every build pass mutates the board through
`pcbnew` or `kicad-tools`. Roughly:

| Capability | SWIG today | `kicad-cli` | IPC API (11+) | kicad-tools |
| --- | --- | --- | --- | --- |
| Load board standalone | yes | n/a | no — live instance | yes (own parser) |
| Mutate board (place, fanout, repair_pads, silk) | yes | no | yes, via live instance | yes |
| Export DSN for Freerouting | yes (`ExportSpecctraDSN`) | no | unknown | possible to add |
| Import SES | yes | no | unknown | possible to add |
| DRC / ERC | — | yes | 11+ | partial |
| Gerber/drill export | — | yes | 11+ | no |

The gerber checker (`scripts/validate_gerbers.py`) is pure text parsing and
depends on none of this.

## Options

**A. Stay pinned — the container *is* the mitigation.** The Dockerfile pins
`kicad/kicad:10.0` by digest; that image builds boards identically for as
long as Docker exists, regardless of what KiCad 11 removes. The deadline
stops being a cliff and becomes drift management: a host on KiCad 11 will
read KiCad 10 boards fine (formats are backward-readable), and
`scripts/doctor.py` already warns on a host/container major mismatch. Cost:
the build toolchain is frozen; new KiCad features never arrive in the
pipeline.

**B. Migrate mutation passes to `kicad-tools`.** Already a credited
dependency; it parses and writes KiCad files with its own s-expression
engine, no KiCad process at all. This is not "hand-editing board files" —
it is a structured library, the thing the CLAUDE.md rule actually permits.
Pass-by-pass migration is possible since the CONTRACT interface is per-pass.
Cost: DSN export / SES import would need implementing or keeping a pinned
KiCad 10 container around just for those two steps.

**C. IPC-under-Xvfb.** Put X11 back in a container and drive a live KiCad
11. Rejected as the primary path — it contradicts the no-GUI rule, adds a
flaky moving part (poll-until-KiCad-answers), and the workaround exists
precisely because upstream hasn't finished headless mode. Reconsider only
if upstream ships true headless IPC (watch the dev-docs IPC pages and
release notes for it).

**D. Hybrid (likely end state).** `kicad-cli` for everything it covers
(exports, DRC — which 11 keeps growing), `kicad-tools` for mutation, and a
digest-pinned KiCad 10 container held only for `ExportSpecctraDSN` /
`ImportSpecctraSES` until B covers them.

## Recommendation

No action now. Option A holds indefinitely and costs nothing today — the
digest pin and the doctor's drift warning were built for exactly this.
Re-evaluate when KiCad 11 reaches beta: check whether headless IPC shipped,
and whether `kicad-cli` grew DSN/SES round-tripping. The trigger to actually
migrate is the first build pass we *want* that needs a KiCad-11-only
feature — not the removal itself.
