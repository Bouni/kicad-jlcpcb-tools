# jlcfootprint

JLC places each part by the EasyEDA footprint of the LCSC part number you picked, not by your KiCad footprint. This package works out a part's CPL rotation from that footprint: it aligns the KiCad footprint's pads onto JLC's pads, and the turn that aligns them is the rotation the CPL needs. The same alignment says whether the picked part fits the pads at all.

Everything here is plain Python 3.9, so it runs inside KiCad's own interpreter. Nothing imports wx or pcbnew, and these modules use the standard library only. Their tests (`tests/test_jlcfootprint_*.py`) need no network and no KiCad.

| Module | What it holds |
| --- | --- |
| `geometry.py` | The `Pad` record, unit and frame conversions, the pad fingerprint, the angle helpers the solver and quality share, and the two conversions to CPL angles |
| `solver.py` | The 2D rigid alignment of two pad sets |
| `quality.py` | The angular RMS, a measure of how well a rotation explains the pads |
| `naming.py` | EasyEDA's footprint naming rule: the pin-1 position a package name encodes |

## Frames and units

- A `Pad` is in millimetres, in the footprint's own frame, with KiCad's Y axis pointing down. Its `rotation` is the pad's own angle within the footprint.
- EasyEDA's classic footprint data is in canvas units of 10 mil (0.254 mm), also with Y down. `easyeda_pads_to_mm` converts it once and does not flip Y (`FLIP_EASYEDA_Y = False`). EasyEDA Pro's footprint text is in mils with Y up and is flipped when it is read.
- A bottom-side footprint's pads are mirrored across the X axis back into the footprint's own frame (`mirror_y`, `mirror_box`), so both board sides compare the same way.
- `rotate` turns +X towards +Y. That is counter-clockwise in the usual Y-up picture, so in KiCad's Y-down frame a positive angle reads clockwise on screen.
- `pad_hash` fingerprints a footprint's pads: names, positions and sizes after each pad's own rotation, rounded to 0.001 mm, in any order. Pin functions are left out, so rewiring a schematic does not change it. It is meant as a persistent key for results worked out from those pads.

## The CPL rotation's sign

The solver's angle is the turn that carries KiCad's pads onto JLC's drawing, which reads clockwise on screen. The CPL counts the other way round: the counter-clockwise turn, as KiCad counts rotation, that carries JLC's drawing back onto KiCad's pads. Those are the same number, so `ccw_correction` only snaps the angle to the nearest 90 degrees and reports it as 0, 90, 180 or 270.

JLC's placement preview is the evidence. A SOIC-8 whose package name carries a -BL token (pin 1 at the bottom-left of JLC's drawing) needs 270 on a footprint at 0 degrees, where the negated angle would be 90. A SOT-23 needs 180, which reads the same either way.

## The alignment solver

`solver.solve_transform` takes two pad sets as `{name: (x, y)}`, pairs pads by name, and returns the rotation and translation that best carry the first set onto the second.

- It is the closed-form 2D Kabsch fit: the best proper rotation comes straight from the cross-covariance of the two centred sets through `atan2`. There is no SVD and no numpy.
- `rotation_deg_raw` is the exact angle and `rotation_deg` the nearest multiple of 90. `residual` is the RMS distance between paired pads at the raw angle.
- Pads are paired by equal names. A `Pad`'s number is a string, so lettered pads (A1, B12) pair too. A pad on only one side is ignored. Unnamed pads (NPTH holes, paste-only copper) cannot be paired; `geometry.named_pads` leaves them out.
- **The mirror flag.** The solver also fits the mirror image of the first set. When that fit's residual is under a tenth of the best rotation's, the result is flagged `is_mirrored`, but the reflection is never applied: a mirrored pin order is a finding about the footprint, never a rotation. A two-pad part fits both ways exactly, and a small floor keeps that from counting as a mirror.
- Fewer than two paired pads, pads that all sit on one point, or a non-finite coordinate give an underdetermined result instead of an exception.

## The quality measure

`quality.angular_rms` asks how well a rotation explains the paired pads. For each pad it compares the pad's bearing from its own side's centroid with its partner's bearing, after taking the rotation away, and returns the RMS of those errors in degrees. A wrong rotation shows up as a large error, and unlike the solver's residual it does not grow with the size of the part.

A pad close to the centroid (within a tenth of the pads' mean distance from it, such as an exposed pad or a DPAK tab) has a bearing that a hundredth of a millimetre can swing by tens of degrees, so it is left out.

## EasyEDA's naming rule

EasyEDA's Footprint Naming Rule puts pin 1's position into the package name: `{family}[_{dimensions}]-{token}`, as in `SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL`. `naming.parse_package_name` reads it and returns a rotation, the rotation's source (`naming_rule`, `family_default` or `none`) and a confidence.

- **Tokens.** TL, TR, BL and BR put pin 1 at a corner; L, R, T and B at an edge; FD, RD and BI give the direction of a two-terminal polarized part.
- **The family** is the name up to its first underscore (or the leading letters of a code such as `LED0603-RD`), cut at its first dimensional segment, so `SMF-L2.9-W1.9-LS3.9-FD` and `SMF_L2.9-W1.9-LS3.9-FD` are both SMF.
- **Only real tokens count.**
  - Dimensional segments (L4.9, W3.9, LS6.0, P1.27, BD6.3, EP) are never orientations.
  - Connectors, headers, relays and other families whose suffix is a free manufacturer part number yield no token at all. Of the MICRO families only the connectors and card sockets (MICRO-USB, MICRO-B, MICRO-SD, MICRO-SIM) are listed, so MICRO-8 and MICRO-MELF keep their tokens.
  - Only the segment between the first and second underscore carries tokens (`VSON-10_L3.0-W3.0-TL-EP_TPS61230DRCR`), except in names that put them before the underscore (`LED0603-RD_GREEN`).
  - A name with no underscore is read only when it is shaped like the rule's names: a family code with digits (`LED0603-RD`) or dimensional segments (`SOP-5-L4.4-W3.6-P1.27-LS7.0-TL`). Anything else is a bare manufacturer part number, whose `-TR` (tape and reel) or `-L` is no orientation (`MICROFC-30035-SMT-TR`).
- **Which token wins.** A corner token (TL, TR, BL, BR) names where pin 1 is drawn, so it wins even over a polarity token: over EasyEDA's library, 81 of the 85 multi-pin drawings whose names carry both put pad 1 at the named corner. Otherwise a polarity token beats an edge token (L, R, T, B), since it names a physical direction, while an edge token on a two-terminal part is only as good as the library's choice of which terminal is pin 1.
- **Cathode-first families.** KiCad's LED and diode footprints number the cathode 1, so those families, the chip diodes named by size (`D0603-RD`) among them, read FD and RD the other way round, at medium confidence. A caller that knows a part is a diode can pass `cathode_is_pin1` for a family the table does not list. Polarized capacitors number the positive terminal 1 and keep the standard reading, and multi-LED arrays (LED-ARRAY and its SMD and TH variants) are never treated as cathode-first.
- **No token.** Standard passives (R, C, L, D, F) default to 0 at low confidence; anything else gets the source `none` and no confidence.
- **Sign.** The table counts the counter-clockwise turn that carries KiCad's footprint onto JLC's drawing, the opposite of the CPL, so `geometry.naming_rule_to_cpl` negates it: the -BL SOIC-8 is 90 in the table and 270 in the CPL.

The rule is a convention, and EasyEDA's library does not always follow it. Over EasyEDA's library, an FD or RD token agrees with the "+" marks drawn on the symbol and footprint on 201 of 203 electrolytic cans, but on only 39 of 45 molded tantalum chips. So the check treats a package name as one source among several, never as the answer on its own.

## What comes next

The package arrives in parts. Still to come:

- **Readers** for EasyEDA's footprint, symbol and drawing records and for KiCad board and library files.
- **The resolver and its gate**: rotation, fit and polarity per part, checked offline against parts whose rotations were read from JLC's placement preview.
- **Background fetch and storage**: the EasyEDA client, a paced worker, the response cache and per-project verdicts.
- **The CPL path**: CPL rotations from the check, behind a setting that is off by default.
- **The UI**: a JLC column with a hover, a detail dialog with a pad overlay, overrides and actions.
- **Variant boards**: the check on boards with KiCad design variants.
