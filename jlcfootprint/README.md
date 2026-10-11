# jlcfootprint

JLC places each part by the EasyEDA footprint of the LCSC part number you picked, not by your KiCad footprint. This package works out a part's CPL rotation from that footprint: it aligns the KiCad footprint's pads onto JLC's pads, and the turn that aligns them is the rotation the CPL needs. The same alignment says whether the picked part fits the pads at all.

Everything here is plain Python 3.9, so it runs inside KiCad's own interpreter. Nothing imports wx or pcbnew, and these modules use the standard library only. Their tests (`tests/test_jlcfootprint_*.py`) need no network and no KiCad: EasyEDA's answers are recorded under `tests/fixtures/jlcfootprint/`.

| Module | What it holds |
| --- | --- |
| `geometry.py` | The `Pad` record, unit and frame conversions, the pad fingerprint, the angle helpers the solver and quality share, and the two conversions to CPL angles |
| `solver.py` | The 2D rigid alignment of two pad sets |
| `quality.py` | The angular RMS, a measure of how well a rotation explains the pads |
| `naming.py` | EasyEDA's footprint naming rule: the pin-1 position a package name encodes |
| `easyeda_parse.py` | EasyEDA's answers read into records: the batch lookup, footprint and symbol documents, the classic per-part response |
| `records.py` | The small records judging a part needs without the parsers: symbol pins, the pin-1 labels, the drawings' marks |

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
- Pads are paired by equal names. A `Pad`'s number is a string, so lettered pads (A1, B12) pair too. A pad on only one side is ignored. A pad with no name (an NPTH hole, paste-only copper, an unnumbered thermal or shield tab) has nothing to pair by, so `geometry.named_pads` leaves it out of the pairing; the pad-box centre still counts an unnumbered soldered pad.
- **The mirror flag.** The solver also fits the mirror image of the first set. When that fit's residual is under a tenth of the best rotation's, the result is flagged `is_mirrored`, but the reflection is never applied: a mirrored pin order is a finding about the footprint, never a rotation. A two-pad part fits both ways exactly, and a small floor keeps that from counting as a mirror.
- Fewer than two paired pads, pads that all sit on one point, or a non-finite coordinate give an underdetermined result instead of an exception.

## The quality measure

`quality.angular_rms` asks how well a rotation explains the paired pads. For each pad it compares the pad's bearing from its own side's centroid with its partner's bearing, after taking the rotation away, and returns the RMS of those errors in degrees. A wrong rotation shows up as a large error, and unlike the solver's residual it does not grow with the size of the part.

A pad close to its side's centroid (within a tenth of that side's mean distance from it, such as an exposed pad or a DPAK tab) has a bearing that a hundredth of a millimetre can swing by tens of degrees, so it is left out.

## EasyEDA's naming rule

EasyEDA's Footprint Naming Rule puts pin 1's position into the package name: `{family}[_{dimensions}]-{token}`, as in `SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL`. `naming.parse_package_name` reads it and returns a rotation, the rotation's source (`naming_rule`, `family_default` or `none`) and a confidence.

- **Tokens.** TL, TR, BL and BR put pin 1 at a corner; L, R, T and B at an edge; FD, RD and BI give the direction of a two-terminal polarized part.
- **The family** is the name up to its first underscore (or the leading letters of a code such as `LED0603-RD`), cut at its first dimensional segment, so `SMF-L2.9-W1.9-LS3.9-FD` and `SMF_L2.9-W1.9-LS3.9-FD` are both SMF. A chip code (R, C, L, D, F or LED and a four-digit size) is its letters on either side of an underscore: `R0603_1` is R, as `R0603` is.
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

## Reading EasyEDA's data

`easyeda_parse.py` reads EasyEDA's answers. It makes no requests of its own.

- **The Pro host** (`pro.easyeda.com`) answers in two steps. The batch lookup (`searchByCodes`) takes up to 200 LCSC numbers and returns one record per part it knows: the symbol uuid, the footprint uuid and the package name (`parse_devices_response`). The per-uuid endpoint then returns one document, a footprint (`parse_puuid_response`) or a symbol (`parse_symbol_response`), in EasyEDA Pro's text form: one JSON array per line, in mils, with Y up. `assemble_record` joins a hit and its two documents into one `ComponentRecord`.
- **The classic host** answers per LCSC number, with the symbol and the footprint in one response (`parse_component_response`). Its shapes are `~`-separated strings in 10-mil canvas units, with Y down. The per-uuid endpoint still serves some footprints in this form, and `parse_puuid_response` reads both.
- **One pad form.** Pro pads are converted to classic canvas units as they are read: mils divided by ten, Y flipped, and a polygon pad's bounding box taken as its size. Everything downstream sees one raw pad geometry, which `geometry.easyeda_pads_to_mm` turns into millimetres. Leading zeros are stripped from numeric pad and pin numbers, so a Pro `01` and a classic `1` are one pad. A pad's layer and shape names keep each format's own codes (Pro writes a through-hole pad's layer as 12 where classic writes 11, and a polygon pad as `POLY` where classic writes `POLYGON`); nothing reads them as one.
- **Pin numbers** are the numbers the symbol displays, not its SPICE field, because the displayed number is the one that matches the footprint's pads. Two parts can share a footprint and still have different symbols, so pin meaning is per part.
- **Status.** Each parser returns `ok`, `none` or `error`, and none of them raises. The body's `success` flag decides. `true` is an answer, and an answer with an empty result is `none`. `false` is EasyEDA's refusal: with its not-found code it is `none` too. `none` means EasyEDA has nothing for the part; JLC's preview shows such a part as a checkerboard. All three per-part and per-uuid endpoints answer a part or document they do not have with HTTP 200 and `{"success": false, "code": 404, "message": "Component not found"}` (recorded under `tests/fixtures/jlcfootprint/easyeda_not_found/`). A classic footprint whose head gives no origin is `error` as well: its pads are read relative to that origin, so without it they would sit about a metre off. A refusal with no code or any other code, a body whose `success` is missing or not a boolean, and any answer that cannot be read are `error`, with the reason kept, so they are asked again later instead of being taken as EasyEDA's answer. A batch answer that is not a list of device records is an error for every code it was asked about, not a miss.

`records.py` holds what the drawings and the symbol's pins say (`DrawingMarks`, `SymbolPin`, the pin-1 label sets), so that code judging a part can read it without importing the parsers.

The recorded EasyEDA answers come from `tools/jlcfootprint/fetch_easyeda_fixture.py`. It paces its requests, backs off when the host refuses, and keeps an answer only when the parser reads it as `ok` or `none`.

## What comes next

The package arrives in parts. Still to come:

- **Readers** for EasyEDA's drawings and for KiCad board and library files.
- **The resolver and its gate**: rotation, fit and polarity per part, checked offline against parts whose rotations were read from JLC's placement preview.
- **Background fetch and storage**: the EasyEDA client, a paced worker, the response cache and per-project verdicts.
- **The CPL path**: CPL rotations from the check, behind a setting that is off by default.
- **The UI**: a JLC column with a hover, a detail dialog with a pad overlay, overrides and actions.
- **Variant boards**: the check on boards with KiCad design variants.
