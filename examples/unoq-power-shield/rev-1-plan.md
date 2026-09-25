# rev-1 plan — UNO Q power & I/O demo shield

**Status:** draft — awaiting sign-off before netlist
**Date:** 2026-08-30

## Scope

A power and I/O demo shield for the Arduino UNO Q, built as this repo's
known-good example board. Screw-terminal VIN (7–24 V) feeds the UNO Q's own
VIN pin and a local 5 V buck; an INA226 reports the whole stack's current
and voltage over I²C; four BSS138 channels level-shift between the UNO Q's
3.3 V headers and 5 V peripherals; Qwiic passthrough; RGB status LED. Cheap
and boring on purpose — its job is to exercise every check in `board.toml`
for a reason a first-time user can see, not to be clever.

## Defect it fixes

First revision — no respin history. One rule here exists because of a defect
on another board: rev D2 of a previous project shipped fiducials as bare
copper dots with no X2 `AperFunction` tag, so gerber-level checking could
not see them. The fiducials gate below requires the tag, verified by
`validate_gerbers` reporting them found.

## Coordinate frame

All coordinates below are the **UNO Q fab frame**: portrait, origin at the
bottom-left of the top view, X across the 53.34 mm width (JANALOG west,
JDIGITAL east), Y along the 68.58 mm length, antenna/USB-C edge at
Y = 68.58. The shield uses the same frame — stacked, shield (x, y) sits
directly over UNO Q (x, y).

## Requirements

Sources: `docs/datasheets/ABX00162-ABX00173-datasheet.pdf` (the UNO Q
datasheet, page numbers as printed), `ABX00162-full-pinout.pdf`,
`ABX00162-schematics.pdf` (sheet numbers), and the fab data inside
`ABX00162-cad-files.zip` (Allegro gerbers + NC drill, the exact files
Arduino manufactures from). Firmware does not exist yet; derived lines are
marked `ASSUMED`.

| # | Requirement | Source |
| - | ----------- | ------ |
| R1 | Outline 68.58 × 53.34 mm, UNO form factor, chamfered corners at the Y=0 end | datasheet §12 p.35 |
| R2 | 4 mounting holes Ø3.2 mm NPTH at (50.80, 53.34), (35.56, 2.54), (7.62, 2.54), (2.54, 54.61) — fixed by the UNO pattern, not ours to choose | drill file `48DMVQAD1_SGB-20250729a4-1-8.drl` T08 block (3.200 mm NON_PLATED ×4); datasheet §12 "4x R1.6" |
| R3 | Header pins Ø1.05 PTH, 2.54 pitch, four strips: JANALOG power 8-pin X=2.54, Y 40.64→22.86 (BOOT, IOREF, RESET, +3V3, +5V, GND, GND, VIN); JANALOG analog 6-pin X=2.54, Y 17.78→5.08 (A0→A5); JDIGITAL 10-pin X=50.80, Y 49.78→26.92 (D21, D20, AREF, GND, D13→D8); JDIGITAL 8-pin X=50.80, Y 22.86→5.08 (D7→D0) | same drill file T04 block (starts + R-code repeats, 32 pins total); pin order from full-pinout p.1 |
| R4 | VIN accepts 7–24 V; the UNO Q's own VIN pin (JANALOG pin 8) takes the same range and feeds the Q's internal buck | datasheet §3.1 p.10, §3.2 p.11 |
| R5 | All shield-facing logic is 3.3 V. A0/A1 not 5 V-tolerant in any mode; ~D3 (PB0) is TT-type, 3.6 V max even in digital mode; other digital pins 5 V-tolerant as inputs only | datasheet §9 p.21, §9.6 note pp.28–29; full-pinout p.1 warning |
| R6 | Shield I²C = I2C2 on D20 (SDA, PB11) / D21 (SCL, PB10). The UNO Q already has 2.2 kΩ pull-ups to 3.3 V on these lines — the shield adds none | datasheet §9.6 p.28; schematics sheet 20 ("IO"), R2642/R2643 2.2K to PWR_3P3V on MCU_I2C2_*_CONN |
| R7 | Wi-Fi PCB antenna occupies X 0.76–22.1, Y 63.9–68.07 (top-left corner, meandered trace). Shield keepout over it, all copper layers, pads included: **X 0–26, Y 61–68.58** | `top.art` gerber copper extraction + 600 DPI render; module WCBN3536A location full-pinout p.1; datasheet §2.2.2 p.7 "shared PCB antenna" |
| R8 | The Q's VIN input already carries reverse-polarity protection (verified at −24 V) and a 24 V TVS — the shield does not duplicate the TVS, but must protect its *own* electronics from reversal | datasheet §3.1 p.10; schematics sheet 20, D26201 PJGBLC24C on DC_IN |
| R9 | +5V header pin is USB VBUS passthrough (power only); IOREF mirrors 3.3 V, output-only, never back-fed. The shield passes both through untouched | datasheet §9.7 p.29 |
| R10 | Max component height on the UNO Q top side is 8.5 mm (the headers themselves); USB-C region ~3 mm. Shield bottom side: no parts (SMT top-only), so stacking clears | datasheet §12 p.35 side view (`ASSUMED`: 8.5 mm = header height, the drawing does not label which part) |
| R11 | Shield 5 V rail budget 1.5 A continuous / 2 A peak for user loads | `ASSUMED` — design choice |
| R12 | INA226 at I²C address 0x40 (A0=A1=GND), measuring total stack VIN current + bus voltage; ALERT open-drain to ~D3 with 10 k pull-up to 3.3 V (D3 is safe for it precisely because ALERT never exceeds 3.3 V — the pin you must not put 5 V on) | INA226 ds pin table + address Table 6-2; `ASSUMED` role |
| R13 | 4 shifted channels on D2, D4, D7, D8 (plain GPIOs; D4 doubles as FDCAN1_TX — CAN unusable while shield IO4 in use, documented) | full-pinout p.1; `ASSUMED` pin choice |
| R14 | RGB LED on ~D9/~D10/~D11 (TIM4_CH3/TIM4_CH4/TIM1_CH3N PWM), active-low, common-anode to shield 5 V | full-pinout p.1; LED polarity XL-1615 ds p.8; `ASSUMED` pin choice |
| R15 | JLCPCB SMT assembly, top side only; screw terminals THT-assembled; header positions DNP (user fits stacking headers — JLC cannot source them) | constraint from Phase 0 |
| R16 | 4-layer: L2 solid GND plane, L3 power plane — both routing-banned | design choice for the `planes` rule |

## Parts

All availability and Basic/Extended status checked against JLCPCB's own
component endpoint on 2026-08-30 (`componentLibraryType`: base/expand).
Datasheets in `docs/datasheets/`.

| RefDes | Part | LCSC | Lib | Price | Stock | Why | Datasheet |
| ------ | ---- | ---- | --- | ----- | ----- | --- | --------- |
| U1 | TPS54331DDAR buck, 3.5–28 V in, 3 A, SO-8 PowerPAD | C90761 | ext | $0.52 | 45k | 24 V max input needs ≥28 V part; **exposed pad is required** (it is what the thermal-via rule checks); TI ds has a full design procedure + layout guide | TPS54331DDAR.pdf (§ "3.5 to 28-V input", PowerPAD) |
| U2 | INA226AIDGSR current/power monitor, VSSOP-10 | C49851 | ext | $0.86 | 81k | senses 0–36 V common mode — 12 V margin over 24 V VIN; ±81.92 mV shunt range; 16 addresses | INA226AIDGSR.pdf (§ "0V to 36V", shunt range table) |
| Q1–Q4 | BSS138LT1G N-FET, SOT-23 | C82045 | ext | $0.044 | 708k | classic bidirectional shifter; V_GS(th) 0.5–1.5 V works from a 3.3 V gate; no edge-rate one-shots to misfire on breadboard wiring | BSS138LT1G.pdf (V_GS(th) 0.5–1.5 V) |
| Q5 | AO4407A P-FET −30 V −12 A, SOIC-8 | C2841482 | ext | $0.13 | 159k | reverse-polarity protection at the terminal; R_DS(on) <13 mΩ @ −10 V → 0.2 W at 4 A | AO4407A.pdf (V_DS −30 V, V_GS ±25 V) |
| D1 | SS34 Schottky 40 V 3 A, SMA | C8678 | **base** | ~$0.02 | 4.9M | TPS54331 is non-synchronous — external catch diode; 40 V > 24 V in, V_F 0.55 V @ 3 A | SS34.pdf |
| D2 | BZT52C12 zener 12 V, SOD-123 | C173429 | ext | ~$0.01 | 358k | clamps Q5's V_GS: at 24 V input the gate would sit 1 V from the ±25 V abs max — no margin without it | BZT52C12.pdf; AO4407A.pdf V_GS spec |
| D3 | XL-1615RGBC-RF RGB LED, 1.6×1.5, common anode | C965840 | ext | $0.02 | 571k | pin 2 = common anode (drawing p.8 — the drawing wins), 20 mA/die max; driven at ~6–9 mA | XL-1615RGBC-RF.pdf p.3, p.8 |
| L1 | SWPA6045S100MT 10 µH, 6045 | C79272 | ext | ~$0.10 | 13k | I_sat 3.20 A > 1.9 A worst-case peak; I_rms 2.45 A > 1.5 A budget; DCR 62 mΩ | SWPA-series-Sunlord.pdf (6045S100MT row) |
| R_sh | HoYLR2512-3W-10mR-1% shunt | C5375464 | ext | $0.055 | 155k | 40 mV @ 4 A — half of INA226 full scale; 0.16 W of a 3 W rating | HoYLR2512-3W-10mR.pdf |
| J1, J2 | KF301-5.0-2P screw terminal (VIN in; 5 V out) | C474881 | ext | $0.10 | 91k | THT, 5.0 mm pitch, the boring reliable choice | KF301-5.0-2P.pdf |
| J3, J4 | SM04B-SRSS-TB JST-SH 4-pin (Qwiic ×2, daisy-chain) | C160404 | ext | $0.21 | 74k | genuine JST; Qwiic passthrough on I2C2 + 3V3 + GND | SM04B-SRSS-TB.pdf |
| — | 0603 passives (10 k pulls ×~10, LED resistors ×3, buck R/C set), fiducials | — | base | — | — | exact base-library codes picked at netlist time with values from the TPS54331 design procedure | — |
| — | headers: 4 strips per R3, DNP | — | — | — | — | plated holes only; user fits an Arduino stacking-header kit | — |

Extended-part lines: 10 (~$3 loading fee each at JLC — noted in the README
cost estimate; every remaining line is Basic or DNP).

**Rejected:**

- *MP1584EN* (C15051) — also SOIC-8-EP and workable, but $2.92 vs $0.52,
  thinner stock, and a far weaker datasheet than TI's for teaching.
- *TPS54202* (C191884, $0.24) — cheaper, but SOT-23-6 with **no exposed
  pad**: it would delete the very thermal-via lesson the board exists for.
- *INA219* — 26 V absolute max bus voltage against a 24 V nominal input; an
  unloaded 24 V wall supply drifts higher than that. INA226's 36 V removes
  the class of failure.
- *TXS0104E* — one part instead of twelve, but its edge-rate accelerators
  glitch on capacitive/breadboard loads, which is exactly how a first-time
  user will wire it. BSS138 pairs are slow and unkillable.
- *WS2812-class addressable RGB* — would have showcased the level shifter
  (its data threshold genuinely needs 5 V logic), but WS2812 timing through
  a BSS138+10k shifter is marginal — a first-build trap, not a demo.
- *AO3401A* (C15127, Basic) — the only Basic P-FET candidate, but SOT-23 at
  ~55 mΩ would dissipate ~0.9 W at 4 A. Not survivable; the Extended
  AO4407A stays.

## Architecture

```
J1 VIN(7-24V) ── Q5 (P-FET, reverse) ── Rsh 10 mΩ ──┬── UNO Q VIN pin (JANALOG 8)
                 gate: 10k to GND,                   └── U1 TPS54331 ── L1 ── 5V rail
                 BZT52C12 clamps VGS      INA226 across Rsh,                 │
                                          VBUS sense downstream,      ┌──────┴──────┐
                                          I²C2 (D20/D21), addr 0x40,  J2 5V out   HV side of Q1-Q4
                                          ALERT → ~D3 (10k to 3V3)                 (LV side → D2,D4,D7,D8;
                                                                                    LV pull-ups to +3V3 pin)
RGB: 5V ── XL-1615 anode; R,G,B cathodes ── 330R ── ~D9/~D10/~D11 (active-low)
Qwiic ×2: I2C2 + 3V3 + GND (pull-ups are the UNO Q's own 2.2k — none here)
All 32 header pins pass through; +5V-USB and IOREF untouched (R9).
```

The INA226 sits **upstream of the split**, so it reports the whole stack's
consumption — UNO Q included — which is the demo: the shield tells you what
the system draws.

Power-source matrix (for the README): USB-C only → Q runs, shield logic
dead (by design, 5 V rail needs VIN); VIN only → everything runs, Q powered
through its VIN pin; both → Q diode-ORs internally (datasheet §3.1), no
contention because the shield never drives the 5 V or 3V3 header pins.

## Capacity math

- **Total VIN current, worst case (7 V input):** UNO Q ≤ 15 W (assumed from
  its USB-C 5 V/3 A contract, §3.1) ÷ 0.9 buck η ÷ 7 V = 2.4 A, plus shield
  5 V × 1.5 A ÷ 0.9 ÷ 7 V = 1.2 A → **3.6 A; design to 4 A**.
- **IPC-2221 trace width, 1 oz external, ΔT = 10 °C:**
  A = (I / (0.048 · 10^0.44))^(1/0.725) mil². 4 A → 110 mil² → **2.0 mm**;
  1.5 A → 28 mil² → 0.53 mm → 0.8 mm floor. VIN routed as ≥2.0 mm /
  pours; `board.toml` enforces the 0.8 mm floor on every power net
  (a floor for the checker, not the design width).
- **Shunt:** 40 mV @ 4 A = 49 % of INA226 ±81.92 mV range; 0.16 W of 3 W;
  LSB 2.5 µV → 0.25 mA resolution.
- **Buck ripple/peaks (worst at 24 V in):** ΔI = 5 V·(1−5/24)/(10 µH·570 kHz)
  = 0.69 A p-p → peak 1.5 + 0.35 = **1.9 A < 3.2 A I_sat**; I_rms ≤ 1.5 A
  < 2.45 A rating.
- **Buck thermals:** ~0.6–0.8 W loss at 1.5 A/24 V (conduction 80 mΩ HS +
  switching, per ds efficiency section); PowerPAD with stitched vias
  θJA ≈ 42 °C/W → ΔT ≈ 25–35 °C. Fine to 60 °C ambient (the Q's own
  operating ceiling, §3.2). Thermal via count per TI layout guideline —
  gate below.
- **Q5:** 13 mΩ @ V_GS −10 V → 0.21 W at 4 A in SOIC-8. V_GS clamped ~12 V
  by D2 (abs max ±25 V — unclamped at 24 V input leaves 1 V margin: not
  acceptable).
- **Level shifter:** 10 k pulls × ~30 pF ≈ 0.3 µs rise → good to ~300 kHz.
  Not for NeoPixels or fast SPI; the README says so.
- **RGB:** (5 − V_F − V_OL)/330 Ω ≈ 6–9 mA per die vs 20 mA max (exact
  values from the ds electrical table at netlist time).

## Board rules this design exercises (→ board.toml)

| Rule | Physical reason here |
| --- | --- |
| thermal vias | U1's PowerPAD is the only path that keeps the buck ≤35 °C over ambient |
| power width ≥0.3 mm + F.Cu pours | VIN carries up to 4 A, but the 0.8 mm the IPC-2221 math implies cannot enter a 0.3 mm pad (the rev-D lesson). Surface power tracks are short hops at 0.3; the current path is the In2 plane plus the bounded F.Cu VIN/VIN_RAW/VIN_PROT pours, whose cross-section satisfies the math. Rationale in `board.toml [nets.power]` |
| mounting holes 4×3.2 | fixed by the UNO drill file, coordinates in R2 |
| planes In1/In2 | solid GND under the switcher; power plane |
| keepout X 0–26, Y 61–68.58 | the UNO Q's Wi-Fi antenna is directly below — copper there detunes the radio |
| fiducials 3, X2-tagged | JLC assembly; and the rev D2 lesson: untagged fiducials are invisible to the checker |

## Gates — all must pass before ordering

Signed off 2026-08-30 except the two that can only close at order time.

- [x] Schematic round-trip diff clean against `netlist.csv`
- [x] ERC clean (0 violations)
- [x] DRC run 5+ times, comparing which violations appear — 5 identical
      runs: 17 violation(s) in 3 kind(s), **0 unconnected**, no unstable
      kinds. Every residual is in the ledger below; **0 clearance
      violations**.
- [x] `validate_gerbers` PASS with this board's `board.toml` — 14 passed,
      0 failed (2 SKIPs: unmatched mask/paste files, no RF budget)
- [x] `check_parity` PASS (added 2026-09-25). `link_schematic` was run
      on this board and its snapshot. It changed metadata only: symbol
      paths, library nicknames, DNP on J5/J10–J13, and board-only on
      H1–H4/FID1–FID3. Gerbers, drill, pos and JLC BOM/CPL re-exported
      identical, dates aside. Before the pass, KiCad reported 61 parity
      items: 54 unlinked footprints and 7 undeclared holes/fiducials.
      After it: KiCad parity 0, independent netlist-vs-pads diff 0, 5 DRC
      runs with 0 shorts and 0 unconnected, and `validate_gerbers` pad nets
      159/159 on 49 parts. Frozen as `release/rev-1/`.
- [x] `check_placement` PASS — 5 passed, 0 failed. Every edge-mating
      connector (J1, J2 screw terminals; J3, J4 Qwiic) reaches its edge and
      has a clear mating corridor, with two acknowledged screw-head grazes
      (see below); antenna keepout clear of tracks, vias, pads and pour in
      the board file as well as the gerbers
- [x] Antenna keepout verified **in the gerbers** (not the KiCad file) —
      `keepouts/unoq_wifi_antenna` clear on all 4 layers
- [x] Thermal vias under U1 ≥ the TI layout guideline count — 4/4 in the
      PowerPAD region
- [x] Fiducials found by the checker (proves X2 AperFunction tags) — 3
      found, 10.13 mm off-axis
- [x] Shield drill pattern matches R2/R3 coordinates exactly — checker
      `drill/mounting-holes` 4×3.2 mm at the UNO drill positions,
      `frame/origin` within 0.050 mm
- [x] Every part in stock (verified against JLC 2026-08-30, all 24 lines)
      — **re-verify at order time**
- [ ] Ordered files committed byte-for-byte (open until the order is
      actually placed)
- [x] README carries the CC BY-SA 4.0 attribution for Arduino's documents

### Change after sign-off — 2026-09-02, silkscreen only

Four `[[silk.text]]` markings were printing on pads, and J5's named the
wrong rail (`3.3V LOGIC` on the 5 V side of the level shifters). Corrected
in `board.toml`; `silk_finish` gained the pad-collision check that should
have caught it — see that pass's docstring.

Copper was NOT re-routed. Only `silk_finish` and the export were re-run,
and the gerbers prove it: F.Cu, In1.Cu, In2.Cu, B.Cu, Edge.Cuts, both mask
and both paste layers, and both drill files are **byte-identical** to the
signed-off package (modulo the generation timestamp). `F_Silkscreen` is
the only file that changed. Gates re-run on the new package: checker 14/0,
DRC ×5 → 17 violations / 3 kinds / **0 unconnected**, unchanged.

## Residual DRC ledger — every remaining violation, explained

17 items in 3 kinds, identical across 5 consecutive DRC runs. None is a
clearance or connectivity defect; each is either the UNO form factor or a
conservative-courtyard graze with real body-to-body space.

| Kind | Count | Explanation |
| --- | --- | --- |
| `copper_edge_clearance` | 4 | All four are J4's pads (Qwiic, JST SM04B side-entry): the connector mounts at the board edge by design, so its pads sit inside the 0.5 mm edge rule. Land pattern per the SM04B datasheet. |
| `pth_inside_courtyard` | 1 | J13 pin 10 inside mounting hole H1's courtyard. Both positions are Arduino's — fixed by the UNO Q drill data — and every real UNO shield carries this same adjacency. |
| `courtyards_overlap` | 12 | 3 are the same UNO form-factor fixture (H1/J13, H2/J4, H3/J1). The other 9 are courtyard-to-courtyard grazes in the deliberately tight power stage (L1 vs C6/C7/C8/C9/D1, D1 vs C2/C3, Q5/D2, R1/R16): bodies clear, conservative courtyards touch. The placement check reports the same set as 13 warnings, 0 errors. |

### Connector-accessibility findings — 2026-09-06

`check_placement.py` was added to the pipeline and run against this board.
It independently rediscovered **two of the three UNO-fixture adjacencies**
already in the ledger above, and said something about them the DRC did not:

| Finding | What DRC called it | What the corridor check adds |
| --- | --- | --- |
| H3 ∩ J1, 3.0 mm² | `courtyards_overlap` | H3's **screw head** (6 mm, not the 3.2 mm hole) reaches ~1.9 mm into the left pole's wire entry on a KF301 screw terminal — an assembly note, not a graze. Use a countersunk M3. |
| H2 ∩ J4, 0.5 mm² | `courtyards_overlap` | Corner graze of the Qwiic cable exit; the ribbon leaves the other way. |

Both are Arduino's fixed hole positions against the only free edge, so they
are unfixable at this outline. Both are declared `accept_blockers` in
`board.toml` with the reason inline — still measured, still printed as
`accepted:`, downgraded rather than hidden. The third fixture adjacency
(H1/J13) involves a stacking header, which mates from above and is
deliberately not modelled by this check.

## Open items (blocking netlist, not blocking sign-off)

- TPS54331 compensation/feedback values from the ds design procedure,
  mapped to Basic-library E-series codes
- Exact base-library codes for all passives
- Thermal-via count + PowerPAD land pattern from TPS54331 ds §layout
- INA226 averaging/conversion config is firmware's job — no hardware impact
