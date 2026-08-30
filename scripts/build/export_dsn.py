#!/usr/bin/env python3
"""Export a Specctra DSN for Freerouting.

Two things happen here that `ExportSpecctraDSN` will not do for you, and both
of them are the difference between a routed board and a broken one:

  * Every layer declared a plane in board.toml `[layers] planes` is rewritten
    from `(type signal)` to `(type power)`. pcbnew exports every copper layer
    as signal, and freerouting will happily route on a plane you declared
    solid — the board-side rule areas are the DRC backstop, this is what
    actually stops the autorouter.
  * Every pad carrying a footprint-local clearance override gets an explicit
    keepout injected, because Specctra's clearance model is per-netclass with
    NO per-pad overrides, so ExportSpecctraDSN silently drops them and the
    router is never told to keep off those pads.

Nothing here knows a net name, a refdes or a board dimension.

board.toml keys consumed:
  [layers] planes          layers to mark (type power); "In1_Cu" -> "In1.Cu"
  [layers] copper          all copper layers (default F_Cu/In1_Cu/In2_Cu/B_Cu)
                           keepouts are injected on copper minus planes

Environment: DSN_KO_STYLE (see below).

Deviation from the source pass: a declared plane layer that is not found in
the exported DSN is a hard failure here, not a warning. It means the router
is about to route on a layer the gerber checker will fail — the same class of
defect the plane declaration exists to prevent.

Not ported:
  * the hardcoded layer names and the per-revision commentary naming which
    inner layer was the RF reference and which the supply plane — that list is
    now `[layers] planes`.
  * a stale "sanity-anchor the convention against a known pad" comment on a
    helper that anchored nothing.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import assert_net_table, emit, fail, load_config, pass_parser  # noqa: E402

# DSN_KO_STYLE: which keepout constructs to inject for clearance-override
# pads.  "full" = keepout + via_keepout (2.x understands both); "plain" =
# keepout only (1.9.0's parser hung on the full set in testing); "none" =
# skip injection entirely (diagnostic only — leaves the override pads'
# rings unprotected).
KO_STYLE = os.environ.get("DSN_KO_STYLE", "full")

DEFAULT_COPPER = ["F_Cu", "In1_Cu", "In2_Cu", "B_Cu"]


def log(*a) -> None:
    print(*a, file=sys.stderr)


def dsn_layer(name: str) -> str:
    """board.toml writes layers KiCad-internal style; the DSN uses dots."""
    return name.replace("_", ".")


def pad_local_clearance(pad):
    """The pad's own clearance override, or None/0 when unset.

    pcbnew returns None when unset (std::optional), and the accessor's
    signature moved between KiCad versions.
    """
    try:
        return pad.GetLocalClearance()
    except TypeError:
        return pad.GetLocalClearance(None)


def main() -> int:
    ap = pass_parser("export_dsn")
    ap.add_argument("--out", required=True, help="path of the .dsn to write")
    args = ap.parse_args()

    cfg = load_config(args.config)
    layers = cfg.get("layers", {})
    copper = list(layers.get("copper") or DEFAULT_COPPER)
    planes = list(layers.get("planes") or [])
    routable = [n for n in copper if n not in planes]
    if not routable:
        fail("every copper layer is declared a plane — nothing left to route on")

    # An empty net table exports a DSN the router reads as "nets to route: 0";
    # it then does nothing and reports success. Catch it before the slow step.
    n_nets = assert_net_table(args.board)

    b = pcbnew.LoadBoard(args.board)
    if b is None:
        fail(f"pcbnew could not load {args.board} (KiCad major mismatch?)")
    n_tracks, n_fps = len(b.GetTracks()), len(b.GetFootprints())
    log("loaded:", b.GetFileName())
    log("tracks:", n_tracks, "footprints:", n_fps, "nets:", b.GetNetCount())

    # ---- surface pours: keep them OUT of the DSN ---------------------------
    # ExportSpecctraDSN writes every zone as a Specctra (plane ...), and
    # freerouting treats a plane as fixed copper the other nets must clear.
    # A ground flood on a routable layer therefore walls off that whole
    # layer and the router runs out of room (measured: 16 nets unfinished
    # on a 2-signal-layer board whose F.Cu carried the GND flood). Surface
    # pours don't need router awareness — the post-route fill re-carves
    # them around whatever copper exists — so remove them from the
    # IN-MEMORY board only (never saved). Pours on declared plane layers
    # stay: they are the (plane) semantics the router must respect.
    plane_ids = {b.GetLayerID(dsn_layer(n)) for n in planes}
    surface_pours = [
        z for z in b.Zones()
        if not z.GetIsRuleArea()
        and not any(z.IsOnLayer(l) for l in plane_ids if l >= 0)
    ]
    for z in surface_pours:
        b.Remove(z)
    if surface_pours:
        log(f"withheld {len(surface_pours)} surface pour(s) from the DSN "
            "(re-poured after routing)")

    ok = pcbnew.ExportSpecctraDSN(b, args.out)
    log("ExportSpecctraDSN ->", ok)
    if not ok:
        fail(f"ExportSpecctraDSN failed writing {args.out}")

    text = Path(args.out).read_text(encoding="utf-8")

    # ---- class names: no commas ---------------------------------------------
    # KiCad 10 exports composite netclass names like "Power,Default". A comma
    # inside an unquoted Specctra token splits it, so freerouting 1.9.0 never
    # binds those nets to their class and routes them at the DEFAULT width —
    # exactly the thin-power failure the gerber checker then flags.
    def _fix_class(m: "re.Match[str]") -> str:
        return "(class " + m.group(1).replace(",", "_")

    text, n_cls = re.subn(r"\(class\s+([^\s()]+)", _fix_class, text)
    if n_cls:
        log(f"sanitised {n_cls} class name(s) (commas -> _)")

    # ---- plane layers: (type signal) -> (type power) ------------------------
    # pcbnew exports every copper layer as (type signal); freerouting will not
    # route on a (type power) layer, so rewrite each declaration.  The
    # board-side rule areas are the DRC backstop; this is what actually stops
    # the autorouter.
    marked = []
    for lname in planes:
        dl = dsn_layer(lname)
        text, n = re.subn(
            r'(\(layer\s+' + re.escape(dl) + r'\s*\(type\s+)signal(\))',
            r"\1power\2",
            text,
        )
        if n:
            log(f"patched DSN: {dl} marked (type power) [{n} instance(s)]")
            marked.append(dl)
        else:
            m = re.search(r'\(layer\s+' + re.escape(dl) + r'[\s\S]{0,120}', text)
            ctx = m.group(0)[:150] if m else f"no {dl} layer block found"
            fail(f"{dl} declared a plane in {args.config} but no "
                 f"(layer {dl} (type signal)) found in {args.out} — freerouting "
                 f"would route on it.\n  context: {ctx}")

    # ---- clearance-override pads: explicit keepouts -------------------------
    # Specctra's clearance model is per-netclass with NO per-pad overrides, so
    # ExportSpecctraDSN silently drops footprint-local pad clearances and the
    # router is never told to keep off those pads (mounting-hole and fiducial
    # rings are the usual carriers).  Inject an explicit keepout +
    # via_keepout circle per such pad on every routable copper layer.
    kos = []
    for fp in b.GetFootprints():
        for p in fp.Pads():
            lc = pad_local_clearance(p)
            if not lc:
                continue
            pos = p.GetPosition()
            r_mm = max(p.GetSize().x, p.GetSize().y) / 2e6 + lc / 1e6
            kos.append((fp.GetReference(), pos.x / 1e6, pos.y / 1e6, r_mm))

    inject = []
    if kos:
        # DSN coordinate system: parse the unit line rather than assuming.
        # KiCad writes "(unit um)" and plain-um coordinates; "(resolution um
        # 10)" is only the granularity, NOT a coordinate multiplier.  Y axis
        # is negated.
        m = re.search(r'\(unit\s+(\w+)\)', text)
        unit = m.group(1) if m else "um"
        try:
            scale = {"um": 1000.0, "mm": 1.0, "mil": 1 / 0.0254, "inch": 1 / 25.4}[unit]
        except KeyError:
            fail(f"{args.out}: unknown DSN unit '{unit}' — refusing to guess a scale")

        def dsn_xy(x_mm, y_mm):
            return x_mm * scale, -y_mm * scale

        for ref, x_mm, y_mm, r_mm in kos:
            dx, dy = dsn_xy(x_mm, y_mm)
            d = 2 * r_mm * scale
            for lname in routable:
                layer = dsn_layer(lname)
                if KO_STYLE in ("full", "plain"):
                    inject.append(
                        f'    (keepout "ko_{ref}_{layer}" (circle {layer} {d:.0f} {dx:.0f} {dy:.0f}))')
                if KO_STYLE == "full":
                    inject.append(
                        f'    (via_keepout "vko_{ref}_{layer}" (circle {layer} {d:.0f} {dx:.0f} {dy:.0f}))')

        # insert before the close of the (structure ...) block
        si = text.find("(structure")
        if not inject:
            log(f"keepout injection skipped (DSN_KO_STYLE={KO_STYLE})")
        elif si < 0:
            fail(f"{args.out}: no (structure) block found — keepouts NOT injected")
        else:
            depth, i = 0, si
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            text = text[:i] + "\n" + "\n".join(inject) + "\n  " + text[i:]
            log(f"injected {len(inject)} keepout shapes for {len(kos)} "
                f"clearance-override pad(s): {[k[0] for k in kos]}")
    else:
        log("no pads with local clearance overrides found (no keepouts injected)")

    Path(args.out).write_text(text, encoding="utf-8")

    emit(
        "export_dsn",
        board=args.board,
        dsn=args.out,
        nets=n_nets,
        tracks=n_tracks,
        footprints=n_fps,
        planes_marked=marked,
        routable_layers=[dsn_layer(n) for n in routable],
        ko_style=KO_STYLE,
        clearance_pads=[k[0] for k in kos],
        keepout_shapes=len(inject),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
