# Native Linux impedance checks

The existing **Native KiCad 10** job in
[the test workflow](../.github/workflows/tests.yml) runs the impedance checks on
Ubuntu 24.04 with KiCad from the official stable 10.0 PPA. It uses the same
system Python, `pcbnew`, wxGTK and virtual environment as the other native KiCad
tests. There is no separate impedance image or Docker job.

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

GitHub retains the `native-kicad` artifact on success and failure. It contains
pytest logs, JUnit and temporary files, plus the smoke's stage logs, `summary.json`,
per-stage results, captures, workbooks, HTML and fabrication ZIPs. Incomplete or
failed stages return a nonzero exit status. Harness unit tests alone do not
establish native behavior; report the actual native run and artifact evidence.

These checks cover Linux GTK/X11 and sample projects. They do not establish
Windows, Wayland, physical-display behavior, Excel/Numbers appearance, electrical
accuracy, or installation through KiCad's PCM UI. The extracted PCM package has
separate archive-content and report-generation tests.
