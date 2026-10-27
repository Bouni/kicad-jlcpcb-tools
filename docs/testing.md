# Running tests

Install Docker and [`act`](https://nektosact.com/installation/), then start Docker.

```sh
python scripts/run_tests.py
```

Runs all seven lanes in `.github/workflows/tests.yml`: ordinary Python, native
wx controls, isolated input, and KiCad 7–10. Architecture follows the Docker
daemon, using AMD64 compatibility lanes where required.

Use `--architecture amd64` to force AMD64 throughout. Logs and artifacts are
saved in the printed results directory. Run with `--help` for output and cache
options.

Tests use tracked files with local edits; stage new files to include them.

See [native impedance checks](linux-impedance-testing.md) for the KiCad 10 smoke
workflow and isolated performance profiles.

Dependency image packages refresh on the first run of each ISO week in UTC,
including local Docker builds. Both builders pass the same weekly revision to
the Dockerfile; an existing layer cache therefore cannot keep an older package
installation indefinitely. To refresh sooner, increment the numeric prefix in
the workflow's `DEPENDENCY_REVISION`. The Ubuntu base image remains pinned.
Hosted jobs always use AMD64; the architecture override applies only to `act`.

## Without Docker

Install the project and its test dependencies in a Python environment:

```sh
python -m pip install '.[dev]'
```

Run ordinary Python tests, real wx controls, isolated input tests, and real KiCad
tests separately:

```sh
python -m pytest -rs -m 'not native_wx and not native_kicad'
python -m pytest -rs --require-native=wx -m 'not native_kicad and not os_input'
python -m pytest -rs --require-native=wx -m os_input
python -m pytest -rs --require-native=kicad --kicad-major=10 -m native_kicad
```

Use Python libraries matching the installed KiCad or wx packages. Set
`--kicad-major` to the installed major (7, 8, 9, or 10). Mouse/keyboard tests
require an isolated desktop with a window manager; Linux native tests can use
Xvfb. These direct commands do not install the native libraries or provide that
desktop.

Required native modes reject missing libraries, empty native selections, and
skipped native tests. The KiCad mode permits three skip categories: design
variant tests on a KiCad without that API, standalone DRC tests on KiCad 10,
whose DRC crashes outside KiCad, and golden-output tests for a KiCad major
without recorded outputs in `tests/golden/kicad_builds.json`.

Report ordinary, native wx, and native KiCad results separately, including the
tested revision, Python/wx/KiCad versions, command, and pass/skip/deselection
counts. A passing test with a fake board does not verify KiCad's file handling,
project state, or native object cleanup. A required workflow lane that was not
run remains unvalidated.
