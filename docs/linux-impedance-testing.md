# Native Linux impedance checks

The existing **Native KiCad 10** job in
[the test workflow](../.github/workflows/tests.yml) runs the impedance checks on
Ubuntu 24.04 with KiCad from the official stable 10.0 PPA. It uses the same
system Python, `pcbnew`, wxGTK and virtual environment as the other native KiCad
tests. There is no separate impedance image or Docker job.

The `kicad10_impedance` marker identifies the generated/saved fixture and CLI
capture/DRC integrations required in that lane. Their file/API and CLI contracts
require KiCad 10. Native KiCad 7–9 lanes deselect only those marked integrations
and retain every other native compatibility check. The KiCad 10 lane selects
all `native_kicad` tests; skipped required tests still fail the lane.

The job runs two complementary checks:

- `pytest --require-native=kicad -m native_kicad` includes the impedance
  rendering, net-class, board-copy, fixture and DRC regressions. The job enables
  `KICAD_CLI` and `KICAD_CAPTURE_PNG`, with disposable default-palette preferences.
  Missing native prerequisites or skipped required tests fail the lane.
- `scripts/linux_native_impedance_smoke.py` runs seven stages in fresh Python
  processes: prerequisites, both example boards, a custom palette, dialogs,
  persistence/report workflows, and the full plugin Generate workflow. A separate
  Xvfb display and Openbox session provide native controls and modal interaction.

The smoke requires matching stable KiCad 10.0.x CLI and Python bindings. It
records their actual versions, fails on missing native APIs, and bounds each
stage. Run it only in a standalone Linux process, never inside PCB Editor.

## Running the workflow on Linux

Use the packages and Python environment from the test workflow. With Xvfb and
Openbox running, invoke:

```sh
python scripts/linux_native_impedance_smoke.py \
  --output /tmp/impedance-native-review --timeout 600
```

The output must be a fresh directory outside the source checkout. The workflow
provides the complete display/window-manager startup command and time limits.
The script copies checked-in sample boards and creates per-stage preferences,
plugin settings and SQLite databases beneath that output. It rejects existing
outputs, source paths and symlink redirection. Source board/project bytes and
native board state are checked across generation.

## Evidence and scope

The Generate stage uses the real plugin constructor and toolbar handlers,
Configure, stackup selection, width calculation, preview approval, Save/reopen,
and Gerber/Excellon/report publication. It checks cancellation, stale responses,
failed saves, report/ZIP failures and disabled regeneration. Catalog and solver
responses are deterministic test data; a live provider check is separate.

The KiCad 10 pytest lane also runs the draft and approved cases in
`test_native_kicad_configure_and_save_budgets`. Their disposable board has
four controlled net classes, twelve 32-segment routes, arcs, mixed widths,
pad-contained stubs, four copper layers, and two reference-plane zones. Each
measured Configure request includes native plane filling, snapshots, matching,
controls, selected-image rendering, and settings reads. Save timing includes the
durable SQLite write, modal dismissal, and capture cleanup. The strict desktop
targets are 450 ms for the first opening and 250 ms for each later opening,
Save, pending Save, and background event-dispatch gap. Background approval
verification is recorded separately.
Saved widths provide deterministic provider results.
`test_native_kicad_first_use_configure_budgets` additionally measures three
fresh processes with feature imports inside the timed Configure action. Each
fresh-process opening has a 450 ms strict target; each Save retains 250 ms. Opening
ends after the selected image's source-checked paint callback. JUnit retains
per-phase function counts and inclusive durations even when a budget fails;
overlapping function durations must not be added together.
The companion control-only cases run in the wx lane and isolate wx timing from
native board and raster work, using the same targets.

Native timing gates default to `KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE=strict`.
Both hosted native CI jobs explicitly select `ci`, which sets regression ceilings
at exactly twice those targets: 900 ms for a first opening, and 500 ms for each
warm opening, Save, pending Save, and background event-dispatch gap. This allowance
accommodates hosted-runner variance; passing it does not establish the strict
desktop target. Set the environment variable on the pytest command to reproduce
either profile; its value is propagated to isolated pytest children and fresh
plain-Python first-use processes. For example, in the prepared native environment:

```sh
KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE=strict python -m pytest \
  --require-native=kicad --kicad-major=10 tests/test_impedance_performance_native.py \
  -m native_kicad
```

Every individual sample is checked; no mean or percentile can hide an outlier.
JUnit retains the raw timings, the selected profile and limits, and
`strict_target_misses` even when the CI ceiling passes. Existing work-count
assertions still require one opening match and one Save snapshot, without Save
matching or rendering. Python-only work budgets remain 250 ms and are unaffected
by the native profile. Inspect the actual native evidence before claiming a
strict target passes.

The native wx jobs retain their JUnit timing evidence as `native-wx-0` and
`native-wx-1` artifacts, including strict-target misses under the CI profile.

GitHub retains each `native-kicad-7` through `native-kicad-10` artifact on success
and failure. They contain pytest logs, JUnit and temporary files. The KiCad 10 artifact
also contains the smoke's stage logs, `summary.json`, per-stage results, captures,
workbooks, HTML and fabrication ZIPs. Incomplete or failed stages return a nonzero
exit status. Harness unit tests alone do not establish native behavior; report the
actual native run and artifact evidence.

These checks cover Linux GTK/X11 and sample projects. They do not establish
Windows, Wayland, physical-display behavior, Excel/Numbers appearance, electrical
accuracy, or installation through KiCad's PCM UI. The extracted PCM package has
separate archive-content and report-generation tests.
