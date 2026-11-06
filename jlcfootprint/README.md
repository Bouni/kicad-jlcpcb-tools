# jlcfootprint

JLC places each part by the EasyEDA footprint of the LCSC part number you picked, not by your KiCad footprint. This package works out a part's CPL rotation from that footprint: it aligns the KiCad footprint's pads onto JLC's pads, and the turn that aligns them is the rotation the CPL needs. The same alignment says whether the picked part fits the pads at all.

Everything here is plain Python 3.9, so it runs inside KiCad's own interpreter. Nothing imports wx or pcbnew, and these modules use the standard library only. Their tests (`tests/test_jlcfootprint_*.py`) need no network and no KiCad: EasyEDA's answers and KiCad's library footprints are recorded under `tests/fixtures/jlcfootprint/`.

| Module | What it holds |
| --- | --- |
| `geometry.py` | The `Pad` record, unit and frame conversions, the pad fingerprint, the angle helpers the solver and quality share, and the two conversions to CPL angles |
| `solver.py` | The 2D rigid alignment of two pad sets |
| `quality.py` | The angular RMS, a measure of how well a rotation explains the pads |
| `naming.py` | EasyEDA's footprint naming rule: the pin-1 position a package name encodes |
| `easyeda_parse.py` | EasyEDA's answers read into records: the batch lookup, footprint and symbol documents, the classic per-part response |
| `records.py` | The small records judging a part needs without the parsers: symbol pins, the pin-1 labels, the drawings' marks |
| `drawing.py` | The `+` marks and the body outline read from EasyEDA's symbol and footprint drawings |
| `boardfile.py` | Footprints, pads and courtyards read from KiCad board and footprint files, without pcbnew |
| `polarity.py` | What kind of two-pad part a part is, and which of its pads is the cathode or the positive terminal |
| `fit.py` | Pad pairing, the placement, the fit grades, JLC's package origin and the courtyard overhang |
| `resolver.py` | One part's verdict: its rotation, fit and polarity |
| `resolving.py` | The resolver called on a parsed EasyEDA record |

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
- **One pad form.** Pro pads are converted to classic canvas units as they are read: mils divided by ten, Y flipped, and a polygon pad's bounding box taken as its size. Everything downstream sees one raw pad geometry, which `geometry.easyeda_pads_to_mm` turns into millimetres. Leading zeros are stripped from numeric pad and pin numbers, so a Pro `01` and a classic `1` are one pad. A pad's layer and shape names keep each format's own codes (Pro writes a through-hole pad's layer as 12 where classic writes 11, and a polygon pad as `POLY` where classic writes `POLYGON`); nothing reads them as one. Every part recorded from both hosts reads the same pads, body box and `+` marks from either (`test_jlcfootprint_both_hosts.py`).
- **Pin numbers** are the numbers the symbol displays, not its SPICE field, because the displayed number is the one that matches the footprint's pads. Two parts can share a footprint and still have different symbols, so pin meaning is per part.
- **Status.** Each parser returns `ok`, `none` or `error`, and none of them raises. The body's `success` flag decides. `true` is an answer, and an answer with an empty result is `none`. `false` is EasyEDA's refusal: with its not-found code it is `none` too. `none` means EasyEDA has nothing for the part; JLC's preview shows such a part as a checkerboard. All three per-part and per-uuid endpoints answer a part or document they do not have with HTTP 200 and `{"success": false, "code": 404, "message": "Component not found"}` (recorded under `tests/fixtures/jlcfootprint/easyeda_not_found/`). A classic footprint whose head gives no origin is `error` as well: its pads are read relative to that origin, so without it they would sit about a metre off. A refusal with no code or any other code, a body whose `success` is missing or not a boolean, and any answer that cannot be read are `error`, with the reason kept, so they are asked again later instead of being taken as EasyEDA's answer. A batch answer that is not a list of device records is an error for every code it was asked about, not a miss.

`records.py` holds what the drawings and the symbol's pins say (`DrawingMarks`, `SymbolPin`, the pin-1 label sets), so that code judging a part can read it without importing the parsers.

The recorded EasyEDA answers come from `tools/jlcfootprint/fetch_easyeda_fixture.py`. It paces its requests, backs off when the host refuses, and keeps an answer only when the parser reads it as `ok` or `none`.

### The drawings

No EasyEDA record says which terminal of a two-pad part is positive. The drawings do: the symbol draws a `+` beside its positive pin, and the footprint draws one beside its positive pad, on the silkscreen or the document layer. JLC's placement preview follows these marks. Where a tantalum's footprint numbers its terminals the other way round, the package name's FD/RD token is wrong and the marks are right.

`drawing.py` reads both drawings, in both formats, each in its own frame and units. Only two-pin symbols and two-pad footprints are read. A `+` is two bars, a horizontal one and a vertical one, on the same layer, and it has to stand alone:

- the bars are alike in length (neither is more than 2.5 times the other) and cross near both their midpoints, which rules out an outline corner and a `T`;
- they are between 5 % and 45 % of the pin span long on a symbol, and between 5 % and 70 % of the pad spacing on a footprint, so a capacitor's plates, a footprint outline and pin stubs are not bars of a `+`;
- on a footprint the lower bound never exceeds 0.75 mm, so the ordinary 30 to 40 mil mark beside a large part's pads is still read;
- no third bar comes near the crossing or the bars' ends, and no polyline or polygon edge comes near the crossing, which rules out a diode's silk arrow;
- on a footprint the crossing sits off the centre (an origin cross names no pad), and every `+` has to be on the same side;
- on a symbol the nearer pin has to be clearly nearer, and every `+` has to name the same pin.

When any of this fails, no terminal is named. The footprint's body outline (Pro layer 48, classic layer 99) is read as a box in millimetres, in the same frame as the pads. Only its straight records are boxed; an arc, a circle or a curved outline on that layer is left out, so a body drawn partly with them gives the box of its straight records, which can only understate it. A classic drawing is boxed about the origin its pads were read against, which the caller must pass.

## Reading KiCad files

The plugin reads a board through pcbnew. The development tools and the tests read KiCad files from plain Python instead: `boardfile.py` parses the footprints of a `.kicad_pcb` or `.kicad_mod` file into their pads (in the footprint's own frame, with the placed rotation taken off), pin functions, LCSC field and courtyard box. The LCSC field is read as the plugin reads a footprint's default assignment, `part_assignments.resolve_assignment` (`boardfile.lcsc_assignment`, and a test holds the two together): only part-number field names count (LCSC, JLC or JLCPCB, with an optional Part, Part Number, PN, Code or the like), the number is upper-cased, and two fields naming different numbers are a conflict, read as no number. The pads are the soldered ones, the same set the plugin keeps when it reads a board through pcbnew: every copper pad that is not an NPTH hole, numbered or not. A paste-only aperture is left out. The alignment pairs pads by name, so an unnumbered pad only counts in the pad-box centre. That centre is taken from the pads' positions and sizes, so it is the centre of pcbnew's pad bounding box except where the copper is not the pad's size at its position: a custom pad's extra primitives, copper offset from its drill, or a pad turned off the 90-degree grid. Over KiCad 10's library the pad sets agree on every footprint, and the centres on all but 385 of the 15,113 footprints with soldered pads: 358 with custom pads, 11 with offset copper, 2 with both and 14 with pads at other angles. The largest difference is 2.25 mm.

`tools/jlcfootprint/kicad_library.py` reads a named KiCad library footprint for the tests. It uses the snapshot recorded under `tests/fixtures/jlcfootprint/kicad/` when there is one, so the tests see the same pads on every machine and run without KiCad. `tools/jlcfootprint/snapshot_kicad_footprints.py` records the snapshots from an installed KiCad, and `test_jlcfootprint_snapshots.py` compares them with the installed library where there is one.

## Judging a part

`resolver.resolve` takes one footprint's KiCad pads (in the footprint's own frame, a bottom-side part mirrored back), its KiCad footprint name, EasyEDA's package name, pads in millimetres and symbol pins, and optionally the drawings' marks and the KiCad courtyard. It returns a `Verdict`: a status, a fit grade, the CPL rotation when one can be derived, how it was found (`method`), a confidence and notes that say why. `resolving.resolve_record` makes the same call from a parsed EasyEDA record.

A part EasyEDA has nothing for, a failed fetch, a pad with a non-numeric coordinate and a side with fewer than two named pads all give `unknown` with no rotation. Otherwise the part takes one of three paths.

### Parts with more than two pads

The pads pair by name and `solver.solve_transform` aligns them; the turn, snapped to 90 degrees, is the rotation (`method` `geometry`).

- A name that covers several pads on one side (KiCad numbers a SOT-223's tab as a second pad 2, a DPAK's tab and the stub of its cut lead both as 2, a switch its four legs 1, 1, 2, 2) pairs pad by pad: a pad first meets one of its own kind (within 1.5 times its area), so a pin meets a pin and a tab meets a tab, and among those the nearest after a first placement solved on the names that are unique on both sides (or, when fewer than two are, on each name's centroid). The order either footprint lists its pads in never matters. All KiCad pads that carry one number are one terminal and are graded together.
- A number drawn as a different count of like pads on the two sides (two pads 1 against one) is aligned on each side's centre for it, so the one pair the pairing chose does not pull the placement half the pads' spacing off. A number whose pads are of two kinds (KiCad's SOT-223 pad 2 is a pin and the tab) keeps its pairs by kind.
- When the numbers do not line up (fewer than two shared names, a mirrored order, or a pad that misses), the pads pair by the schematic's pin functions against the symbol's pin names instead: names compare whole, less the `_<number>` suffix KiCad appends to a pin function (`D_2` on pad 2 is `D`, but `DATA_IN` is not `DATA_OUT`), a name found once on each side pairs, at least two must pair, and one pad left on each side pairs by elimination. KiCad's `TO-252-3_TabPin2` numbers the tab 2 where JLC's DPAK drawing numbers it 3; its `D` function pairs it all the same. A pairing by function is medium confidence and its note lists every number that differs.
- Without that, a mirrored pin order is red (`mirror`), never solved. A part with as many pads as JLC's whose pads all land at some angle when their names are ignored is red (`numbering`), with the angle the package itself would take: the numbering differs from JLC's part, so the package's angle would put the wrong pins on the wrong pads.
- A part whose pads all carry one number on either side (a standoff's or a shield's legs) is unknown: any turn that puts each pad on some pad would pass, so the names cannot say which way round it goes.
- Only two shared names, or a rotation the package name's token disagrees with, lowers the confidence to medium, with a note.

### Two-pad parts by meaning

A part with two JLC pad numbers that is polarized, or whose package name carries an FD/RD token, aligns by terminal meaning and never by pad number (`method` `polarity`). Two numbers are two terminals however many pads a drawing splits them into (a PLCC-4 LED's 1, 1, 2, 2): each terminal votes and aligns as one pad at its centre, the centroid of its pads of the largest kind, and the fit is graded on the real pads. When the two terminals' centres coincide (a coin cell's + tabs either side of its - pad), meaning gives no axis, so the pads align by number instead, with a note; a numbering that swapped the terminals would not fit. The meaning is then held to that numbering: the KiCad pin functions and the vote (without the name token, which names a side such terminals do not have) must put the reference terminal on pads the alignment paired, by number or, when the numbers differ, by pin function. A reference that pairs with the other terminal is red, since no turn can swap the terminals, or yellow when KiCad's pad 1 was only assumed; a tied vote is red; with nothing to vote the alignment stands.

- **The kind.** `polarity.part_kind` reads the evidence from the most to the least reliable: the KiCad footprint's name (`D_`, `LED_`; `CP_`, `C_Elec`, `Tantalum`) and its pin functions (`K`/`A`), EasyEDA's family (the cathode-first diode families, the polarized capacitor families), the battery, buzzer and microphone footprints and families, the symbol's pin labels, and last a `+` drawn in either drawing. A diode's reference terminal is its cathode; a capacitor's and any other polarized part's is its positive terminal.
- **The KiCad side.** A pin function naming the reference terminal, or naming the other terminal on the other pad, gives the reference pad (`K_1`, `+_1` and `anode` all count). Two pads claiming one terminal are contradictory and the part is unknown. With no usable function pad 1 is assumed (K on a diode, + on the other polarized parts), at medium confidence, and a note says so.
- **The EasyEDA side: the vote.** Up to four sources each name the pad that carries the reference terminal: the package name's FD/RD token (forward direction puts the marking band, the cathode or the negative end, on the right of JLC's drawing), the symbol's pin-1 label, the symbol's `+` and the footprint's `+`. The label and the symbol's `+` count only on a two-pin symbol, where a pin 1 labelled `C` is a cathode rather than a transistor's collector. The majority decides. A tie, such as one token against one mark, is red: "EasyEDA data inconsistent", and no guess. No source at all is unknown: "polarity unknown". A vote that loses is named in a note and lowers the confidence to medium, and so does a decision by the marks alone or an unchecked token on the molded-chip tantalums, the one family whose token is least reliable (over EasyEDA's library it agrees with the marks on 39 of 45 of them, against 201 of 203 electrolytic cans). A bidirectional (`BI`) part has no reference terminal and aligns by axis.
- Once both reference pads are known the two pads align like any pair, and that pairing must fit; a polarized part is never turned to fit by shape.
- **The pin-1 light.** The vote also says what JLC's pad 1 is. When it is not what KiCad's pad 1 is, the rotation is still right but JLC's pin-1 marker will sit on the other terminal, so the part is yellow with a note saying not to renumber the footprint.
- An FD/RD token on a part that is not polarized (a marked inductor) puts KiCad's pad 1 where the token puts pin 1, read the way a capacitor's token is read, which need not be JLC's pad 1, and the part is flagged non-polar. Only the token votes for such a part, since a label on it is a switch's A/B or a transistor's C/E, and a token that cannot be applied (pads drawn upright) leaves the part to its axis, as if it had none.
- A token whose angle disagrees with the result lowers the confidence to medium. The name's angle assumes KiCad's pad 1 is the reference terminal, so when the schematic puts it on pad 2 the note says so; otherwise the note says the KiCad footprint is drawn non-standard.

### Two-pad parts without polarity

A non-polar part with two pads on each side aligns by its axis, and 180 degrees is irrelevant: the rotation is given modulo 180 (`method` `axis`). Two pads that carry one number on either side (a mounting hole's, a single-way terminal block's, or a resistor's on a footprint whose two pads are one terminal) are not two terminals, and the part is unknown, as a four-legged standoff is.

### The second pairing

Two pads pair two ways, so both are graded: KiCad's two pads against JLC's, and against JLC's swapped. One pairing fits clearly better when it fits and the other does not, or when both fit and its worst pad's overlap is at least `fit.SHAPE_MARGIN` (0.1) higher. Mirror-symmetric pads give exactly 0, and so does every two-pad part on the gate boards; a coin-cell holder whose two pads differ in shape gives 0.263.

- A non-polar part whose pads fit one pairing clearly better takes that pairing and a full turn (`method` `shape`).
- A polarized part keeps its meaning's rotation, but when the swapped pairing fits clearly better it is yellow: one of the two footprints may draw its terminals at the other end.

### Fit and status

After the placement every JLC pad must land on KiCad copper of its number, overlapping it by at least half the smaller pad's area; KiCad pads of one number that overlap (a thermal-via footprint's vias and back-side pad, numbered like its exposed pad) count their shared copper once. A pad whose centre falls outside the inner 80 % of the KiCad pad lands tight. A JLC pad left over from a number that paired (a third pad 2 where KiCad draws two) must land on that terminal's copper, or on KiCad copper no JLC pad claims (JLC's tab 2 on KiCad's tab 4), never on another terminal's; only at the centre and on no copper at all is it an exposed pad the footprint lacks, as any central pad is (some packages number theirs like a pin, as a MOSFET's drain pad). A JLC pad whose number pairs with nothing is graded against the nearest KiCad pad; extra KiCad copper is never a misfit, and a custom-shaped KiCad pad is not judged.

| `fit` | Meaning |
| --- | --- |
| `fits` | Every JLC pad lands on KiCad copper |
| `fits_larger_pads` | It fits, and KiCad's pads are at least 1.5 times JLC's (hand-solder pads) |
| `fits_tight` | The pads overlap, but a JLC pad's centre sits at the edge of its KiCad pad |
| `count`, `pitch` | A JLC pad misses: `count` when the pad counts differ, `pitch` when they are equal |
| `numbering`, `mirror` | The package fits but the numbering differs, or runs the other way |
| `no_data` | Nothing to align |

The status is **green** when the part fits and its rotation is known, **yellow** when it fits with a caveat (tight pads, a central pad the KiCad footprint lacks, JLC's pin-1 marker on the other terminal, the shapes preferring the other pairing), **red** when it does not fit, EasyEDA's data contradicts itself or a part's terminals cannot be turned to their meaning, and **unknown** when there is nothing to judge. Only green and yellow carry a rotation.

- **The body caveat.** When the footprint drawing has a body outline and the KiCad footprint a courtyard, the courtyard is moved by the placement and compared with JLC's body. An overhang of more than 10 % of the courtyard's shorter side (never less than 0.15 mm nor more than 1.5 mm) is noted; the status and the rotation stand.
- **The package origin.** `Verdict.origin` is the point the placement carries onto the drawing's own origin: where JLC's package origin sits in the footprint frame, for green and yellow verdicts only.

## The gate

Two KiCad boards under `tests/fixtures/jlcfootprint/boards/` were uploaded to JLC, and for each part checked the rotation JLC's placement preview settled on was written down (D5's by construction, as its note says: it is D4 with the anode wired to pad 1, which the preview cannot show). The gate is that the resolver, run offline on the same boards and the recorded EasyEDA answers, reproduces every one of them.

- `corner_case/` has 47 footprints from KiCad's own library: SOT-23s at all four rotations and on both sides, SOIC-8, QFN, SOT-223, DPAK, DIP-8, USB-C, diodes, LEDs, electrolytic and tantalum capacitors, resistors, a 0603 part on 0402 pads, a wrong pick (SOT-23-5 on SOT-23-6 pads) and a SOT-23 whose JLC drawing numbers its pins clockwise (`-CW`). `tools/jlcfootprint/make_corner_case_board.py` builds it with pcbnew from `cases.csv` and `blank.kicad_pcb`.
- `pin_functions_and_marks/` has 16: DPAKs, most of whose JLC drawings number the tab differently, paired by pin function; tantalums whose polarity comes from their `+` marks and tokens, one on the bottom side; and an electrolytic on a footprint too small for its body, beside the same part on one that fits.

**The truth files.** `tests/fixtures/jlcfootprint/truth.csv` belongs to the corner-case board and `truth_pin_functions_and_marks.csv` to the second. Each row is one part:

- `observed_rotation` is JLC's angle for the part after it was aligned in the preview. Blank means the part must get no derived rotation (a wrong pick, or a refusal by design).
- `origin_dx_mm` and `origin_dy_mm`, where present, pin the package origin the resolver derives, in the footprint frame, within 5 µm; blank means no origin. They are the resolver's own output, rounded, kept so that a change to it shows: the preview shows no offset that small. The origin the preview does settle is the TO-252-2's, 0.72 mm from its pad box (`test_jlcfootprint_origin.py`).
- `note` says why, and is not read.

The preview does not show an angle. It turns a part 90 degrees counter-clockwise per press of the space bar, so the corner-case board was uploaded with the raw placed angles and the presses were written down per part (`truth_presses.csv`); `tools/jlcfootprint/truth_from_presses.py` adds them to the uploaded CPL's angles. Q7's row and the origin columns were then added to `truth.csv` by hand, so the tool alone does not rebuild that file; the gate tests fail if a row or an origin goes missing.

**The validator.** `tools/jlcfootprint/validate_board.py` reads the board with `boardfile`, without pcbnew, and each part's recorded EasyEDA answer: the classic responses by default, or the Pro host's batch, footprint and symbol documents with `--pro-fixtures`. It resolves every part, works out the CPL rotation the plugin's `Fabrication` would write (a bottom-side angle mirrored as 180 minus the angle, then the correction added), and compares it with the truth, within 0.001 degrees either way round the circle. A part aligned by its axis, or a marked inductor, compares modulo 180; a part aligned by pad shape fits one way round only and compares modulo 360. Any disagreement, or a truth row for a part that is not on the board, exits 1. `--report` writes the per-part table; the committed `pre_upload_report.txt` beside each board is that table, so a change in any verdict, note or origin shows up as a diff.

```bash
B=tests/fixtures/jlcfootprint/boards; FX=tests/fixtures/jlcfootprint; T=tools/jlcfootprint
python3 $T/validate_board.py $B/corner_case/corner_case.kicad_pcb --truth $FX/truth.csv --report /tmp/r.txt && cmp /tmp/r.txt $B/corner_case/pre_upload_report.txt
python3 $T/validate_board.py $B/corner_case/corner_case.kicad_pcb --truth $FX/truth.csv --pro-fixtures $FX/easyeda_pro --report /tmp/r.txt && cmp /tmp/r.txt $B/corner_case/pre_upload_report.txt
python3 $T/validate_board.py $B/pin_functions_and_marks/pin_functions_and_marks.kicad_pcb --truth $FX/truth_pin_functions_and_marks.csv --pro-fixtures $FX/easyeda_pro --report /tmp/r.txt && cmp /tmp/r.txt $B/pin_functions_and_marks/pre_upload_report.txt
```

All 47 parts on the corner-case board agree, from the classic answers and from the Pro host's, with the same report; on the second board all 16 agree. `tests/test_jlcfootprint_gate.py` runs the same comparisons in the test suite, compares each report with the committed one byte for byte, and pins a few of the verdicts.

## What comes next

The package arrives in parts. Still to come:

- **Background fetch and storage**: the EasyEDA client, a paced worker, the response cache and per-project verdicts.
- **The CPL path**: CPL rotations from the check, behind a setting that is off by default.
- **The UI**: a JLC column with a hover, a detail dialog with a pad overlay, overrides and actions.
- **Variant boards**: the check on boards with KiCad design variants.
