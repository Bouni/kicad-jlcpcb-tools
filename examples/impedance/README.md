# Two impedance review boards

| Open in KiCad | Target | Connectors | Circuits |
| --- | ---: | --- | ---: |
| [Single-ended RF / 50 ohm](single-ended-50-ohm.kicad_pcb) | 50 Ω | SMA, two per circuit | 14 |
| [USB differential / 90 ohm / 8 mil pair gap](usb-differential-90-ohm.kicad_pcb) | 90 Ω | USB-C, two per circuit | 14 |

Each project is one continuous six-layer board with two labelled banks:
**noncoplanar** (surface microstrip / inner stripline) and **CPWG**
(surface / buried coplanar waveguide). Each bank contains top, inner and
bottom 2 mm and 120 mm controlled cores, plus a 150 mm three-layer route.
Lengths describe controlled cores; connector launches add electrical length.
Every circuit has unique nets; both members of each USB pair share one
highlight. Separate noncoplanar/CPWG net classes are visible in Board Setup.
USB controlled-run edge spacing is 8 mil (0.2032 mm).

Open the PCB, **Fill All Zones** (B), and select F.Cu, In2.Cu or B.Cu.
Use **Inactive Layer View Mode → Dim** to see nearby board context.
All six copper layers have GND pours. The adjacent reference layers are
In1.Cu for top, In1.Cu + In3.Cu for inner, and In4.Cu for bottom.
Keep each `.kicad_pro` and `.kicad_dru` beside its board: custom rules
hold noncoplanar same-layer GND zones 3 × the actual trace width away.
CPWG uses its close ground gap and stitching vias instead.

These are workflow/capture examples, **not solver-qualified impedance
coupons or USB-certified designs**. Consolidated geometry has not been
tested or run through DRC; review the workflow first. Canonical sources
omit generated fill caches. Plugin settings remain in the project database;
`manifest.json` is example metadata only.

[Design notes](../../docs/rf-impedance-fixtures.md) ·
[Embedded footprint sources and license](showcases/source-footprints/LICENSE.md)

Regenerate these two projects:

```sh
myenv/bin/python scripts/generate_rf_impedance_fixtures.py
```

Isolated matrix cases remain available on demand, outside this folder:

```sh
myenv/bin/python scripts/generate_rf_impedance_fixtures.py --legacy-matrix --output /tmp/isolated-rf-examples
```

The generator only writes its named artifacts. `--retire-legacy` moves
known old standalone board projects to a recoverable temporary backup;
unrelated files and the pinned footprint assets are left alone.
