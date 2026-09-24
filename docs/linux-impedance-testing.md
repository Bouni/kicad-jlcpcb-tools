# Native Linux impedance checks

Run the complete seven-lane workflow with `python scripts/run_tests.py`; see
[testing instructions](testing.md) for architecture and output options. The
existing KiCad 10 dependency image supplies matching stable CLI/Python bindings,
wxGTK and Xvfb. No separate impedance image or job is needed.

KiCad 10 selects every `native_kicad` test. KiCad 7–9 exclude only the
`kicad10_impedance` fixtures and CLI integrations, retaining all other native
compatibility checks. Each lane uses disposable default-palette preferences.
KiCad 10 then runs seven smoke stages in
fresh Python processes: prerequisites, both example boards, a custom palette,
dialogs, persistence/report workflows, and the complete plugin Generate workflow. Missing prerequisites, unexpected
skips, failed stages and incomplete evidence fail the lane.

## Direct smoke run

Use the Python environment and packages from the test workflow. With Xvfb and
Openbox running, invoke:

```sh
python scripts/linux_native_impedance_smoke.py \
  --output /tmp/impedance-native-review --timeout 600
```

Output must be a fresh directory outside the checkout. The smoke copies source
boards into disposable storage and rejects existing outputs, source paths and
symlink redirection. The workflow provides the desktop and retains its complete
external output under the lane artifacts, including failed runs. Run standalone
on Linux, never inside PCB Editor.

Lane artifacts contain runtime information, JUnit and logs. KiCad 10 also retains
the smoke's ordered `summary.json`, per-stage results, captures, workbooks and HTML.
The dialog and workflow stages exercise real constructors, events, reopening,
independent board settings and report publication. Generate covers the real plugin
constructor and toolbar, Configure, width calculation, approval, Save/reopen,
fabrication publication, cancellation and failure cases. Catalog and solver
responses are deterministic test data.

Native Configure/Save tests use `KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE=ci` in the
workflow: 900 ms for a first opening and 500 ms for warm openings, Save, pending
Save and background event gaps. The default `strict` profile uses half those
limits. Every sample is checked and JUnit retains strict-target misses. Passing
CI does not establish the strict desktop target; Python budgets remain 250 ms.

These checks cover Linux GTK/X11 and disposable sample projects. They do not
establish Windows, Wayland, physical-display behavior, Excel/Numbers appearance,
electrical accuracy or PCM installation. Archive contents and report generation
have separate tests.
