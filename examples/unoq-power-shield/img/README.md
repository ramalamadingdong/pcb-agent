# Board renders

Generated from `../unoq-power-shield.kicad_pcb` with `kicad-cli pcb render`.
Regenerate after any layout change — these are pictures of a board file, and a
stale one is a lie about the current design.

**Run these against a NATIVE KiCad, not the container.** The `kicad/kicad`
base image ships no 3D model packages, so a render from inside the container
silently comes out bare — every part missing, no error. That failure mode is
also the trick used for `board-bare.png` below: point the model variable at a
path that doesn't exist and nothing resolves.

```bash
BRD=examples/unoq-power-shield/unoq-power-shield.kicad_pcb
IMG=examples/unoq-power-shield/img

# board-iso.png — populated, isometric (the repo's hero shot)
kicad-cli pcb render -o $IMG/board-iso.png --width 1200 --height 1200 \
  --side top --quality high --background opaque --rotate "-22,0,-18" --zoom 0.85 $BRD

# board-assembled.png — populated, straight down
kicad-cli pcb render -o $IMG/board-assembled.png --width 1000 --height 1260 \
  --side top --quality high --background opaque --zoom 0.95 $BRD

# board-bare.png — same framing, no components
kicad-cli pcb render -o $IMG/board-bare.png --width 1000 --height 1260 \
  --side top --quality high --background opaque --zoom 0.95 \
  -D KICAD10_3DMODEL_DIR=/nonexistent $BRD
```

`-D KICAD10_3DMODEL_DIR` must match the major version the board file
references (grep the `.kicad_pcb` for `3DMODEL_DIR`) — on KiCad 9 boards the
variable is `KICAD9_3DMODEL_DIR` and the override silently does nothing under
the wrong name, giving you a populated render where you wanted a bare one.

The committed PNGs are quantized to a 256-colour adaptive palette (~65%
smaller, visually identical on these flat-shaded renders).
