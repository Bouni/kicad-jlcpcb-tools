# KiCad IPC Plugin Conversion Plan

## Overview

Convert the existing SWIG-based Action Plugin into a KiCad IPC Plugin while preserving backward compatibility with the Action Plugin path for KiCad 8.x.

## Current State

-   ✅ IPC client transport (`ipc_client.py`)
-   ✅ IPC adapters (`ipc_impl.py`)
-   ✅ Provider-based routing (`KicadProvider`)
-   ✅ Export abstraction (`ExportPlan`) with CLI fallback for KiCad 9-10
-   ✅ Main dialog (`mainwindow.py`) — currently SWIG-only
-   ⚠️ Plugin entry point (`plugin.py`) — currently Action Plugin only

## Key Decisions Made

**1. UI Strategy: Preserve UX**

-   Keep main dialog, launch as subprocess from IPC plugin action
-   No refactoring of dialog itself; minimal integration work

**2. Entry Points: Parallel Support**

-   Keep: `plugin.py` as SWIG Action Plugin (KiCad 8.x users)
-   Add: `ipc_plugin_main.py` as IPC plugin entry point (KiCad 9.0+)
-   Both can coexist; KiCad will load both if present

**3. Dependency Management**

-   Declare `kicad-python` as a plugin dependency (KiCad's venv auto-installs)
-   Keep all existing dependencies compatible with Python 3.9+

**4. Export Support & Fallback**

-   Support KiCad 9.0+ (IPC available, export via CLI fallback)
-   Support KiCad 11.0+ (IPC export commands natively available)
-   CLI fallback tested and working in `common/test_ipc_export_plan.py`

---

## Implementation Phases

### Phase 1: Plugin Manifest & Entry Point

**Status**: ✅ Complete (Commit: `51ec626`)

**PR-301: Create `plugin.json` and IPC plugin entry point**

Files created:

-   [x] New `plugin.json` (in plugin root)
-   [x] New `ipc_plugin_main.py`
-   [x] Update `pyproject.toml` (add `kicad-python` dependency, entry point)

Tasks:

1.  [x] Write `plugin.json` following KiCad schema:
    -   Plugin metadata (name, version, description, author)
    -   Python runtime declaration
    -   Single action: "Generate BOM & Gerbers"
    -   Dependencies: `kicad-python`, `jlcpcb-tools` (self)

2.  [x] Create `ipc_plugin_main.py`:
    -   Entry point reads `KICAD_API_SOCKET` env var
    -   Creates `KiCadIPCClient` with socket path
    -   Calls `KicadProvider.create_adapter_set(prefer_ipc=True)`
    -   Launches `mainwindow.MainWindow`

3.  [x] Update `pyproject.toml`:
    -   Add `kicad-python>=0.1.0` to `dependencies`
    -   Add `console_scripts` entry: `kicad-jlcpcb-ipc = ipc_plugin_main:main`
    -   Updated Python requirement to `>=3.9`

Validation:

-   [x] `plugin.json` validates against JSON schema
-   [x] Dry-run: `python ipc_plugin_main.py` with no socket fails gracefully with clear error
-   [x] All 218 tests pass

---

### Phase 2: IPC Client Initialization

**Status**: ⬜ Not started

**PR-302: Refactor `ipc_client.py` for plugin context**

Files to modify:

-   [ ] `ipc_client.py` (improve error handling, token support)
-   [ ] `kicad_api.py` (update `KicadProvider` to pass token)
-   [ ] New `common/test_ipc_init.py` (test IPC initialization paths)

Tasks:

1.  [ ] Enhance `KiCadIPCClient.__init__()`:
    -   Accept optional `token` parameter (read from `KICAD_API_TOKEN` env var if not provided)
    -   Pass token to API calls where required
    -   Improve error messages for socket not found, connection refused

2.  [ ] Update `KicadProvider.create_adapter_set()`:
    -   Accept `launch_context` parameter (default: 'swig', 'ipc', or 'auto')
    -   When `launch_context='ipc'`:
        -   Read `KICAD_API_SOCKET` env var
        -   Construct `KiCadIPCClient(socket_path, token=...)`
        -   Return adapters backed by IPC

3.  [ ] Add tests in `common/test_ipc_init.py`:
    -   Test IPC initialization with mock socket
    -   Test fallback to SWIG when IPC unavailable
    -   Test token passing

Validation:

-   [ ] `pytest common/test_ipc_init.py` passes
-   [ ] Full test suite still passes (218 tests)

---

### Phase 3: Plugin Directory Structure & Packaging

**Status**: ⬜ Not started

**PR-303: Set up plugin directory layout for KiCad discovery**

Files to modify:

-   [ ] Create or confirm `plugin_manifest/plugin.json` location
-   [ ] New `plugin_manifest/py.typed` (for type hints)
-   [ ] Update `pyproject.toml` (package data, entry point)
-   [ ] Create `PLUGIN_README.md` (for plugin distribution)

Tasks:

1.  [ ] Decide: plugin lives in repo root or in subdirectory?
    -   KiCad expects: `~/.local/share/KiCad/9.0/plugins/kicad-jlcpcb-tools/plugin.json`
    -   Build process: copy files to that location during install

2.  [ ] Update packaging:
    -   `pyproject.toml` `[project]` section: name as plugin identifier
    -   `[tool.setuptools.package-data]`: include `plugin.json`, `py.typed`
    -   Document installation: "Copy built package to KiCad plugins directory"

3.  [ ] Create `PLUGIN_README.md`:
    -   Installation steps (OS-specific paths)
    -   Minimum KiCad version (9.0)
    -   Debugging instructions (enable trace, check logs)
    -   Fallback to SWIG if IPC unavailable

Validation:

-   [ ] `python -m build` succeeds
-   [ ] Wheel/tarball contains `plugin.json` and all required files
-   [ ] Manual: extract and place in KiCad plugins dir, verify action appears

---

### Phase 4: Integration & End-to-End Test

**Status**: ⬜ Not started

**PR-304: E2E IPC plugin validation**

Files to create:

-   [ ] New `common/test_ipc_e2e.py` (integration test)
-   [ ] New `tests/fixtures/ipc_plugin_boot.py` (startup simulator)

Tasks:

1.  [ ] Create end-to-end test:
    -   Mock `KICAD_API_SOCKET` and `KICAD_API_TOKEN` env vars
    -   Simulate plugin boot sequence
    -   Verify `ipc_plugin_main.main()` successfully creates adapters
    -   Call one adapter method (e.g., `get_board()`) to confirm IPC path works

2.  [ ] Document known issues:
    -   Virtual environment setup timing (actions delay on first load)
    -   Debugging: check `~/.cache/KiCad/9.0/python-environments/kicad-jlcpcb-tools/`
    -   Fallback behavior when IPC fails

Validation:

-   [ ] `pytest common/test_ipc_e2e.py` passes
-   [ ] Full test suite passes
-   [ ] Manual: Install plugin in KiCad 9.0+, verify action appears and works

---

### Phase 5: Documentation & Deprecation Path

**Status**: ⬜ Not started

**PR-305: Update docs, add migration guide**

Files to modify/create:

-   [ ] Update `README.md` with IPC plugin info
-   [ ] New `docs/IPC_PLUGIN_GUIDE.md` (setup, debugging, troubleshooting)
-   [ ] New `docs/SWIG_ACTION_PLUGIN_LEGACY.md` (deprecation notice)
-   [ ] Update `pyproject.toml` (version bump for "IPC-capable" release)

Tasks:

1.  [ ] Update `README.md`:
    -   Highlight IPC plugin as primary method (KiCad 9.0+)
    -   Keep SWIG Action Plugin instructions for legacy users (KiCad 8.x)
    -   Installation: two paths, both supported

2.  [ ] Create `docs/IPC_PLUGIN_GUIDE.md`:
    -   Step-by-step install (download release, extract to plugins dir)
    -   Expected behavior (action appears after venv setup ~10-30 seconds)
    -   Troubleshooting: check trace log, API log, console messages
    -   How to force environment rebuild

3.  [ ] Create `docs/SWIG_ACTION_PLUGIN_LEGACY.md`:
    -   Mark SWIG plugin as legacy (but still functional and supported)
    -   IPC version is recommended for KiCad 9.0+
    -   Both can coexist

4.  [ ] Version bump in `pyproject.toml`:
    -   Update version (e.g., `0.9.0 → 0.10.0`) to signal IPC support

Validation:

-   [ ] Docs are clear and actionable
-   [ ] Version bump properly marked in `pyproject.toml`

---

### Phase 6: Remove Implementation Plan

**Status**: ⬜ Not started

**PR-306: Cleanup**

Files to remove:

-   [ ] Delete `docs/ipc-plugin-conversion-plan.md`

Validation:

-   [ ] All milestones closed
-   [ ] All phases implemented and tested

---

## Milestones & Acceptance

**MS-09** (after Phase 1) ✅ Complete

-   [x] `plugin.json` exists and validates
-   [x] `ipc_plugin_main.py` runs without error (or fails gracefully)
-   [x] `pyproject.toml` declares IPC dependencies

**MS-10** (after Phase 2) ⬜ Not started

-   [ ] `KicadProvider` supports `launch_context='ipc'`
-   [ ] IPC initialization tests pass
-   [ ] Full test suite green (no regression)

**MS-11** (after Phase 3) ⬜ Not started

-   [ ] Built package includes `plugin.json` and all required files
-   [ ] Installation instructions clear

**MS-12 — IPC Plugin Ready** (after Phase 4) ⬜ Not started

-   [ ] E2E test confirms IPC bootstrap works
-   [ ] Manual test: action appears in KiCad toolbar after venv setup
-   [ ] Manual test: action executes successfully and generates output

**MS-13** (after Phase 5) ⬜ Not started

-   [ ] Documentation complete
-   [ ] Both SWIG and IPC paths documented
-   [ ] Troubleshooting guide in place

**MS-14 — Architecture Complete** (after Phase 6) ⬜ Not started

-   [ ] All implementation phases complete
-   [ ] All tests passing
-   [ ] This file deleted

---

## Risk Factors & Mitigations

| Risk | Mitigation |
|---|---|
| KiCad's venv setup timing unpredictable | Document expected delays, add debug logging |
| Token handling not clear from docs | Test with `kicad-python` examples, reach out to KiCad devs if stuck |
| Plugin discovery in unexpected location | Hard-code expected paths per platform in docs |
| Breaking change in `kicad-python` API | Pin `kicad-python>=0.x,<1.0` in `pyproject.toml` |
| SWIG path still used (dual launch) | Mark SWIG as deprecated, clearly document IPC path |

---

## Git Conventions

-   Branch naming: `track4/pr{YYY}-short-description` (e.g. `track4/pr301-ipc-plugin-manifest`)
-   Commit style: Conventional Commits scoped by subsystem (`plugin`, `ipc-init`, etc.)
-   This plan is updated as progress is made
