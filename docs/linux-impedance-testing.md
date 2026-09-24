# Native Linux impedance checks

Run the complete seven-lane workflow with `python scripts/run_tests.py`; see
[testing instructions](testing.md) for architecture and output options. The
existing KiCad 10 dependency image supplies matching stable CLI/Python bindings,
wxGTK and Xvfb. No separate impedance image or job is needed.

Every KiCad lane runs `pytest --require-native=kicad -m native_kicad` with
disposable default-palette preferences. KiCad 10 then runs six smoke stages in
fresh Python processes: prerequisites, both example boards, a custom palette,
dialogs, and persistence/report workflows. Missing prerequisites, unexpected
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
independent board settings and report publication. They do not yet exercise the
complete plugin Generate toolbar workflow.

These checks cover Linux GTK/X11 and disposable sample projects. They do not
establish Windows, Wayland, physical-display behavior, Excel/Numbers appearance,
electrical accuracy or PCM installation. Archive contents and report generation
have separate tests.
