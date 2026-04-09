# KiCad IPC Migration Plan

## Guiding Principles

- Small PRs, single concern each, independently revertible.
- Behavior parity before architecture; fallback paths stay until milestone gates close.
- Each PR must leave the test suite fully green before merge.
- Milestone IDs (`MS-xx`) are stable labels for acceptance gates — they are separate from PR sequence (`PR-yyy`).
- Solo cadence: one PR in-flight at a time.

---

## Track 1 — Split Existing Migration into Reviewable PRs

The existing large migration commit is split into five focused PRs in dependency order.

**PR-001: Adapter core**
- Files: `kicad_api.py`
- Introduces `BoardAPI`, `FootprintAPI`, `GerberAPI`, `UtilityAPI`, `KicadAdapterSet`, `KicadProvider`, and all SWIG implementations. No other files change.
- Commit message: `refactor(adapter): add KiCad adapter contracts and SWIG provider`

**PR-002: Standalone stubs + contract tests**
- Files: `standalone_impl.py`, `common/test_kicad_adapters.py`
- Adds `StubBoardAdapter`, `StubFootprintAdapter`, `StubGerberAdapter`, `StubUtilityAdapter`, `StubAdapterSet`, and `create_adapter_set()`. Contract tests verify stub shapes match ABC expectations.
- Commit message: `test(standalone): add adapter stubs and contract tests`

**PR-003: Entrypoint wiring**
- Files: `plugin.py`, `__main__.py`
- Plugin startup calls `KicadProvider.create_adapter_set()` and passes result to main dialog. Standalone entry passes `create_adapter_set()` from stubs. Fail-fast behavior on missing SWIG.
- Commit message: `refactor(entrypoint): wire adapter set into plugin and standalone entry`

**PR-004: UI migration**
- Files: `mainwindow.py`
- Constructor accepts `adapter_set`; direct `pcbnew` calls replaced with adapter methods throughout. `self.pcbnew` attribute removed.
- Commit message: `refactor(ui): migrate main dialog to adapter set`

**PR-005: Fabrication / store / helpers**
- Files: `fabrication.py`, `store.py`, `helpers.py`
- Fallback paths removed from fabrication (fail-fast on missing adapter). Store reads footprint metadata via adapter. Obsolete SWIG helper functions removed from helpers.
- Commit message: `refactor(core): migrate fabrication, store, and helpers to adapter access`

### Track 1 Milestones

**MS-01** (closes after PR-002)
- [ ] Adapter boundary exists and is independently tested.
- [ ] `pytest common/test_kicad_adapters.py` passes.
- [ ] No behavior change anywhere.

**MS-02** (closes after PR-004)
- [ ] Main UI path runs entirely through adapter set.
- [ ] Full test suite green.
- [ ] `mainwindow.py` contains no direct `pcbnew` imports.

**MS-03** (closes after PR-005)
- [ ] Fabrication, store, and helpers use adapter paths.
- [ ] `fabrication.py` contains no direct `pcbnew` imports.
- [ ] Full test suite green.

---

## Track 2 — Seal Remaining SWIG Leaks

These PRs close the remaining direct `pcbnew` call sites in business logic identified after Track 1.

**PR-006: Add missing `BoardAPI` methods**
- Files: `kicad_api.py`, `standalone_impl.py`
- Add `get_copper_layer_count()`, `get_aux_origin()`, `get_footprints()` to `BoardAPI` ABC. Implement in `SWIGBoardAdapter` and `StubBoardAdapter`.

**PR-007: Add missing `GerberAPI` methods**
- Files: `kicad_api.py`, `standalone_impl.py`
- Add `set_skip_plot_npth_pads()` and `set_drill_format()` to `GerberAPI`. Implement in `SWIGGerberAdapter` and `StubGerberAdapter`.

**PR-008: Route remaining fabrication SWIG calls**
- Files: `fabrication.py`
- Replace all remaining direct `self.board.*` and footprint SWIG calls with adapter methods from PR-006 and PR-007.

**PR-009: Route remaining mainwindow / store SWIG calls**
- Files: `mainwindow.py`, `store.py`
- Remove any remaining raw `pcbnew` object access; route through existing adapter methods.

**PR-010: Enforce ABC inheritance on stub adapters**
- Files: `standalone_impl.py`
- Make `StubBoardAdapter`, `StubFootprintAdapter`, `StubGerberAdapter`, `StubUtilityAdapter` formally inherit from their ABCs so the type-checker and CI catch contract drift automatically.

### Track 2 Milestone

**MS-04** (closes after PR-010)
- [ ] `grep -rn "from pcbnew" --include="*.py"` returns only `kicad_api.py`.
- [ ] Static type-checker finds no ABC violations in any adapter.
- [ ] Full test suite green.

---

## Track 3 — C→B IPC Migration

### Phase C: IPC for Non-Export Paths

**PR-011: IPC transport client**
- Files: new `ipc_client.py`
- Thin socket/JSON-RPC client with `KiCadIPCClient.is_available()` defaulting `False`. No behavior change; IPC path is feature-gated off.

**PR-012: IPC board + footprint + utility adapters**
- Files: new `ipc_impl.py`, `common/test_kicad_adapters.py`
- `IPCBoardAdapter`, `IPCFootprintAdapter`, `IPCUtilityAdapter` implementing existing ABCs. Contract tests confirm these satisfy the same ABC shape as SWIG counterparts.

**PR-013: Provider launch-context routing**
- Files: `kicad_api.py`, `plugin.py`
- `KicadProvider.create_adapter_set()` selects IPC vs SWIG by launch context (`KICAD_API_SOCKET` / IPC runtime), then applies version capability guard (IPC only on supported KiCad versions), with soft fallback to SWIG and explicit startup log. `GerberAPI` stays SWIG throughout this phase.

**MS-05** (closes after PR-013)

- [ ] Plugin starts correctly under both SWIG (no IPC launch context, e.g. v8.0 Action Plugin) and IPC (IPC plugin launch context on supported versions) paths.
- [ ] Startup logs clearly state selected backend and capability set.
- [ ] Export path unchanged; all existing tests green.

### Phase C Export Parity Gate

**PR-014: Parity test suite (BOM, CPL, Gerber, Drill)**
- Files: `common/test_kicad_adapters.py`, new `tests/fixtures/minimal.kicad_pcb`, new `tests/fixtures/gerber_golden/`
- BOM/CPL: parametrized over stub and SWIG backends against the same fixture board. Assert identical output (floats within ±0.001 mm tolerance).
- Gerber inventory: assert expected file set generated for 2-layer fixture.
- Normalized Gerber comparison: strip volatile headers, then compare remaining content line-for-line against checked-in goldens.
  - Strip from `.gbr`: `%TF.CreationDate,…*%`, `%TF.GenerationSoftware,…*%`, `G04 Created by KiCad…*`, `G04 #@$ CreationDate,…*`
  - Strip from `.drl`: `; DRILL file {…} date …`, `; #@! TF.CreationDate,…`, `; #@! TF.GenerationSoftware,…`
- Note: Gerber/Drill tiers require `kicad-cli`; BOM/CPL and option-call mock tiers run in any CI environment.
- `pygerber` (PyPI) is the recommended library for structural Gerber X2 parsing if deeper aperture/coordinate comparison is needed.

**MS-06 — Export Parity Gate** (closes after PR-014)
- [ ] BOM output identical across stub and SWIG backends.
- [ ] CPL output identical within float tolerance.
- [ ] Gerber file inventory correct for fixture board.
- [ ] Normalized Gerber and Drill golden comparisons pass.

> MS-06 is a required safety net before any export path is modified in Phase B.

### Phase B: Backend-Neutral ExportPlan

**PR-015: `ExportPlan` abstraction + `SWIGExportPlan`**
- Files: new `export_api.py`, `fabrication.py`
- `ExportPlan` ABC with `generate_gerbers()` and `generate_drill_files()`. `SWIGExportPlan` is a mechanical refactor of current fabrication export logic. `Fabrication` delegates to the plan object. No `IPCExportPlan` yet.

**PR-016: `IPCExportPlan`**
- Files: `export_api.py`
- `IPCExportPlan` wraps KiCad IPC export commands with kicad-cli fallback. Version-gated; fail-fast with descriptive error on unsupported KiCad versions.

**PR-017: Remove SWIG-only export path**
- Files: `fabrication.py`, `kicad_api.py`
- Remove `GerberAPI` SWIG implementation from main adapter path; export owned entirely by `ExportPlan`. Compatibility shims in `SWIGGerberAdapter` removed.

### Track 3 Milestones

**MS-07** (closes after PR-016)
- [ ] `ExportPlan` ABC satisfied by both `SWIGExportPlan` and `IPCExportPlan`.
- [ ] MS-06 parity tests still green against both plan implementations.
- [ ] Gerber output file lists identical between both plan backends.

**MS-08 — Architecture Complete** (closes after PR-017)
- [ ] No `pcbnew` imports remain outside `kicad_api.py` and export modules.
- [ ] Full test suite green.
- [ ] SWIG export path fully removed.
- [ ] This file (`docs/ipc-migration-plan.md`) can be deleted.

---

## Risk Controls

- Create a `backup/<timestamp>` branch before any git rebase or split operation.
- Rollback policy: revert single PR if breakage is isolated; revert to last closed milestone if cross-PR breakage appears.
- Per-PR pre-merge checklist:
  - [ ] Scope unchanged from PR description.
  - [ ] Tests added/updated for touched behavior.
  - [ ] Manual smoke against one known project before merge.
- Fallback paths must remain available until their milestone gate explicitly closes them.

---

## Git Conventions

- Branch naming: `track{N}/pr{YYY}-short-description` (e.g. `track1/pr001-adapter-core`)
- Commit style: Conventional Commits scoped by subsystem (`adapter`, `standalone`, `ui`, `entrypoint`, `core`, `ipc`, `export`)
- Safety backup branches: `backup/<YYYY-MM-DD>-<description>`
