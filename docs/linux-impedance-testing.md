# Native Linux impedance checks

Run the complete seven-lane workflow with `python scripts/run_tests.py`; see
[testing instructions](testing.md) for architecture and output options. The existing
KiCad 10 dependency image supplies stable CLI/Python bindings, wxGTK and Xvfb.
No separate impedance image or job is needed.

KiCad 10 selects every `native_kicad` test, including `kicad10_impedance` fixtures
and CLI capture/DRC integrations. KiCad 7–9 exclude only that marker and retain
all other native compatibility checks. Missing prerequisites, unexpected skips,
failed stages and incomplete evidence fail the lane.

`scripts/native_kicad_runner.py` first runs the three native timing-budget cases
in isolation, then overlaps two complete-module pytest workers with the full
seven-stage smoke workflow. The RF verifier modules share one worker to reuse
fixture preparation. The runner verifies the complete collected inventory and
merges the three unchanged JUnit reports. Each process owns private state and a
separate desktop; Generate and dialogs start first within the two-worker smoke.

The smoke exercises prerequisites, both example boards, a custom palette,
dialogs, persistence/report workflows, and the real plugin Generate workflow.
It covers Configure, stackup/width selection, spacing approvals, Save/reopen,
reports, fabrication ZIPs, cancellation and publication failures. Catalog and
solver responses are deterministic test data; live provider checks are separate.

## Direct smoke run

Use the Python environment and packages from the test workflow:

```sh
python scripts/linux_native_impedance_smoke.py \
  --output /tmp/impedance-native-review --timeout 600 --workers 2
```

Output must be a fresh directory outside the checkout. Workers create their own
Xvfb/Openbox sessions and copy source boards into disposable storage. Existing
outputs, source paths and symlink redirection are rejected. Source bytes and
native board state are checked across generation. The workflow runs in external
temporary storage and retains its complete output under the lane artifacts,
including failed runs.

## Isolated performance profiles

Choose one profile per run. These use the same dependency image and omit the
unrelated KiCad 10 suites; the complete seven-lane run remains required for final
validation. Direct `act` commands need the Docker connectivity options described
by `act --help` for the local Docker setup.

```sh
act workflow_dispatch -j tests --matrix lane:native-kicad-10 \
  --input generate_benchmark=true --input generate_runs=3

act workflow_dispatch -j tests --matrix lane:native-kicad-10 \
  --input smoke_benchmark=true

act workflow_dispatch -j tests --matrix lane:native-kicad-10 \
  --input source_snapshot_probe=true
```

Generate measures the complete acceptance group in independent fresh processes,
including initialization, fixtures, both approval passes, cold reopening, reports,
failure cases and cleanup. Every run must meet the 60-second target. Use
`generate_runs=1` while iterating. The smoke profile measures all seven stages
sequentially. Both profiles share a prepared Xvfb/Openbox desktop across their
fresh stage processes. Neither includes dependency installation in stage timings.
Normal functional smoke uses two workers; its potentially contended stage times
must not be compared with isolated measurements.

The source profile runs source-evidence regressions, then records complete
acquisition, uncached construction, cold/warm reads and raw-setter invalidation
in `source-probe/result.json`. Every warm read still acquires fresh evidence.
Inclusive phase timings overlap, so compare wall time rather than summing them.

Native Configure/Save timing tests default to
`KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE=strict`: 450 ms for a first opening and
250 ms for warm openings, Save, pending Save and background event gaps. The
workflow explicitly uses `ci`, allowing exactly twice those limits. Every sample
is checked; JUnit retains raw timings, function counts, the selected profile and
strict-target misses. A CI pass does not establish the strict desktop target.
Python-only work budgets remain 250 ms. Set the profile on a pytest command to
reproduce either native ceiling.

## Retained evidence

Each lane artifact contains setup/runtime information, JUnit and logs. KiCad 10
also retains orchestration manifests, phase reports and the smoke's ordered
`summary.json`, per-stage receipts, captures, workbooks, HTML and fabrication ZIPs.
Cancellation and timeouts fail the lane and clean up owned processes. Inspect
actual native receipts before claiming behavior or a timing target is verified.
Failed or cancelled local runs retain partial reports in `output/partial-native/`.

These checks cover Linux GTK/X11 and disposable sample projects. They do not
establish Windows, Wayland, physical-display behavior, Excel/Numbers appearance,
electrical accuracy or installation through KiCad's PCM UI. PCM archive contents
and report generation have separate tests.
