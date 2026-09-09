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

from fnmatch import fnmatch

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


def footprint_local_clearance(fp):
    """The FOOTPRINT's clearance override, or None/0 when unset.

    Not the same thing as a pad override, and not reachable through one: KiCad
    resolves a footprint-level `(clearance ...)` for every pad in it without
    writing it onto the pads, so pad_local_clearance() returns None for all of
    them and the DSN never hears about it.

    Stock library footprints carry these. `Package_SO:TI_SO-PowerPAD-8` ships
    `(clearance 0.2)`, which is why the TPS54360B's own COMP and EN escapes came
    back as ten DRC clearance errors at 0.16-0.19 mm: every one of them cleared
    the 0.15 mm board rule the router was given and violated the 0.2 mm rule
    KiCad actually enforces there.
    """
    try:
        return fp.GetLocalClearance()
    except TypeError:
        return fp.GetLocalClearance(None)


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
    # Nets that own copper the router does not lay. Collected BEFORE the
    # surface pours are withheld below, or the set comes back short.
    pour_nets = {z.GetNetname() for z in b.Zones()
                 if not z.GetIsRuleArea() and z.GetNetname()}
    pour_nets |= {p.get("net") for p in (cfg.get("route", {}).get("pours") or [])}
    pour_nets |= {z.get("net") for z in
                  ((cfg.get("zones") or {}).get("pour") or [])}
    pour_nets.discard(None)

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

    # ---- router margin over the DRC constraint ------------------------------
    # The netclass clearance IS KiCad's DRC minimum. A router given exactly
    # that number routes gaps at exactly that number, and micrometre
    # rounding through the DSN/SES round-trip lands a hair under — every
    # such squeeze becomes a DRC clearance violation. Give the router
    # +0.01 mm so routed gaps clear the constraint with margin.
    def _bump_clearance(m):
        return f"(clearance {float(m.group(1)) + 10:.1f}"

    text, n_clr = re.subn(r"\(clearance\s+([\d.]+)", _bump_clearance, text)
    if n_clr:
        log(f"bumped {n_clr} DSN clearance rule(s) by 10 um (router margin)")

    # ---- floor the clearance at the strictest footprint-local rule ----------
    # Specctra's clearance model is per-netclass with no per-object override, so
    # a footprint-local clearance cannot be expressed as such. The keepout trick
    # used below for pads is wrong here: those pads have to stay REACHABLE, and
    # walling them off is what left +5V_ARM1 unroutable when a fiducial ring
    # landed on J4. Raising the general rule to the strictest local clearance on
    # the board is the only expression the format has.
    #
    # Only UNTYPED rules are floored. `(clearance N (type smd_smd))` is the
    # pad-to-pad gap, which the router cannot change and which fine-pitch
    # escapes need left alone.
    # FOOTPRINT-level overrides only. A PAD-level override is already handled,
    # and handled better, by the keepout injection below: that walls off the one
    # pad instead of slowing the whole board down. Taking the max over both is a
    # measured mistake -- the fiducials carry a 0.6 mm PAD clearance, which
    # floored every rule on the board to 0.61 mm, and freerouting came back with
    # 296 segments instead of 1166 and 46 unconnected instead of 9.
    #
    # A footprint whose pads carry their own overrides is skipped entirely, for
    # the same reason: the keepouts already cover it.
    strict = {}          # netname -> (clearance_um, ref that demands it)
    for fp in b.GetFootprints():
        if any(pad_local_clearance(p) for p in fp.Pads()):
            continue
        value = footprint_local_clearance(fp)
        if not value:
            continue
        want = value / 1000.0 + 10        # same 10 um router margin
        for p in fp.Pads():
            nm = p.GetNetname()
            # A net that owns a pour is fed by copper the router does not lay,
            # and it reaches the whole board -- widening it widens everything.
            if not nm or nm in pour_nets:
                continue
            if want > strict.get(nm, (0, None))[0]:
                strict[nm] = (want, fp.GetReference())

    if strict:
        # Put just these nets in their own class. Flooring the GENERAL rule was
        # the obvious move and it is wrong: measured, 0.16 -> 0.21 mm board-wide
        # bought U10's ten clearance errors and cost twelve unconnected nets,
        # because a 0.5 mm-pitch escape that fits at 0.16 does not fit at 0.21.
        # These nets are local to the footprint that demands the clearance, so
        # a class of their own costs the rest of the board nothing.
        want_um = max(v for v, _r in strict.values())
        names = sorted(strict)
        pat = re.compile(r"\((class\s+\S+)((?:\s+(?:\"[^\"]*\"|[^\s()\"]+))*)",
                         re.S)
        moved = []

        def _strip(m):
            head, body = m.group(1), m.group(2)
            kept = []
            for tok in body.split():
                if tok.strip('"') in strict:
                    moved.append(tok.strip('"'))
                    continue
                kept.append(tok)
            return "(" + head + ("\n      " + " ".join(kept) if kept else "")

        text = pat.sub(_strip, text, count=0)
        if moved:
            # Carry the padstack the other classes use. Without a (circuit
            # (use_via ...)) freerouting has no via to place for these nets,
            # and SW is the buck's switching node -- it needs them.
            uv = re.search(r"\(circuit\s*\(use_via\s+([^\s)]+)\s*\)\s*\)", text)
            circuit = (f"\n      (circuit (use_via {uv.group(1)}))"
                       if uv else "")
            # The class needs the WIDEST width any of its nets is entitled to,
            # not whatever width happened to appear first in the file. Copying
            # the first (rule (width ...)) picked up kicad_default's 250 um and
            # routed SW -- the buck's switching node, and a [nets.power] net --
            # at 0.25 mm, which validate_gerbers fails as "power/width below
            # 0.3mm". Widening a short local signal net costs nothing;
            # narrowing a power net is a fab-level defect.
            pwr_pats = list((cfg.get("nets", {}).get("power", {})
                             or {}).get("patterns") or [])
            route_cfg = cfg.get("route", {}) or {}
            w_sig = float(route_cfg.get("track_width_mm") or 0.2) * 1000
            w_pwr = float(route_cfg.get("power_track_width_mm") or 0.3) * 1000
            w = max(w_pwr if any(fnmatch(n, p) for p in pwr_pats) else w_sig
                    for n in names)
            w = f"{w:.0f}"
            block = ("    (class local_clearance\n      "
                     + " ".join(names)
                     + circuit
                     + f"\n      (rule (width {w}) (clearance "
                     + f"{want_um:.1f}))\n    )\n")
            # append inside (network ...), just before its closing paren
            ni = text.find("(network")
            depth, i = 0, ni
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            text = text[:i] + block + "  " + text[i:]
            demanders = sorted({r for _v, r in strict.values()})
            log(f"moved {len(moved)} net(s) into DSN class 'local_clearance' at "
                f"{want_um:.1f} um for {demanders}: their footprint carries a "
                f"local clearance Specctra cannot express per-object. The "
                f"general rule is left alone -- flooring it costs fine-pitch "
                f"escapes elsewhere on the board.")
            log(f"  nets: {', '.join(names)}")

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

    # ---- board edge: inset the routable boundary ----------------------------
    # KiCad enforces `min_copper_edge_clearance` (0.5 mm by default on every
    # KiCad 10 board) but the DSN carries NO edge rule -- the boundary is just a
    # polygon, and freerouting keeps only its own copper clearance from it. On
    # this board that put an EXP_SCL_C run 0.3654 mm from the right edge against
    # a 0.5 mm constraint: three DRC errors from one track.
    #
    # There is nowhere in Specctra to say "keep 0.5 from the outline", so the
    # boundary handed to the router is shrunk instead. The inset is the full
    # edge clearance rather than the difference from the copper clearance: what
    # a router keeps from a boundary is not a documented number, and over-
    # reserving a fraction of a millimetre of board rim costs nothing.
    #
    # CHECK PAD REACH before raising this. Every pad must stay inside the inset
    # boundary or the router cannot reach it; on this board the closest pad
    # (D2.1) is 0.95 mm from the edge, so 0.5 mm leaves 0.45 mm of margin.
    edge_mm = float((cfg.get("route") or {}).get("edge_clearance_mm") or 0.0)
    if not edge_mm:
        log("NOTE: [route] edge_clearance_mm is unset, so the DSN carries no "
            "edge rule at all and the router will lay copper right up to its "
            "own clearance from the outline. KiCad's default DRC constraint is "
            "0.5 mm -- set the key to match it.")
    else:
        m = re.search(r"(\(boundary\s*\(path\s+\S+\s+\d+\s+)([-\d.\s]+?)(\))",
                      text, re.S)
        if not m:
            fail(f"{args.out}: [route] edge_clearance_mm is set but no "
                 f"(boundary (path ...)) was found to inset")
        nums = [float(v) for v in m.group(2).split()]
        pts = list(zip(nums[0::2], nums[1::2]))
        xs = sorted({p[0] for p in pts})
        ys = sorted({p[1] for p in pts})
        # Coordinates are in the DSN's own unit; parse it rather than assume.
        um = re.search(r"\(unit\s+(\w+)\)", text)
        uname = um.group(1) if um else "um"
        try:
            uscale = {"um": 1000.0, "mm": 1.0,
                      "mil": 1 / 0.0254, "inch": 1 / 25.4}[uname]
        except KeyError:
            fail(f"{args.out}: unknown DSN unit '{uname}' — refusing to guess")
        d = edge_mm * uscale
        if len(xs) == 2 and len(ys) == 2:
            # Axis-aligned rectangle: pull each side toward the interior.
            def _inset(v, lo, hi, delta):
                return v + delta if v == lo else v - delta
            new = [(_inset(x, xs[0], xs[1], d), _inset(y, ys[0], ys[1], d))
                   for x, y in pts]
            body = "  ".join(f"{x:.0f} {y:.0f}" for x, y in new)
            text = text[:m.start(2)] + body + text[m.end(2):]
            log(f"inset the DSN boundary by {edge_mm:.3f} mm "
                f"({(xs[1] - xs[0]) / uscale:.1f} x {(ys[1] - ys[0]) / uscale:.1f} "
                f"-> {(xs[1] - xs[0] - 2 * d) / uscale:.1f} x "
                f"{(ys[1] - ys[0] - 2 * d) / uscale:.1f} mm) so routed copper "
                f"meets the board's edge-clearance constraint")
        else:
            # Not a rectangle. Shrinking a general polygon correctly needs a
            # real offset, and a wrong one either leaks copper to the edge or
            # eats the board. Say so instead of guessing.
            log(f"WARNING: outline is not an axis-aligned rectangle "
                f"({len(pts)} points, {len(xs)} distinct x, {len(ys)} distinct "
                f"y) — boundary NOT inset. Copper may land within "
                f"{edge_mm:.3f} mm of the edge and DRC will flag it.")

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
