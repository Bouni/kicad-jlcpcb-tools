# Project Guidelines

## Overview

KiCad plugin for generating JLCPCB fabrication/assembly files (Gerber, Excellon, BOM, CPL) and managing LCSC part assignments. Supports KiCad v7, v8, v9.

## Architecture

```
UI Layer (wxPython)     → mainwindow.py, partselector.py, partdetails.py, settings.py
Data Layer (SQLite)     → store.py (project.db), library.py (parts-fts5.db)
Business Logic          → fabrication.py, schematicexport.py, corrections.py
API Clients             → lcsc_api.py, common/jlcapi.py
Common Module           → common/ (reusable: componentdb, partsdb, jlcapi, filemgr, translate)
DB Build CLI            → db_build/jlcparts_db_convert.py
```

- **Entry points:** `plugin.py` (KiCad plugin), `__main__.py` (standalone/testing), `standalone_impl.py` (KiCad API stubs)
- **One main class per module.** `helpers.py` holds shared utilities, `events.py` holds custom wx events.
- **`common/`** is independently importable — no dependency on the plugin UI layer.

## Build and Test

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest common/

# Lint
ruff check .

# Rebuild parts database
python db_build/jlcparts_db_convert.py
```

## Code Style

- **Linter:** Ruff with extensive rule set — see `pyproject.toml` `[tool.ruff.lint]` for full config
- **Imports:** isort via Ruff — stdlib → third-party → local; `force-sort-within-sections`, `combine-as-imports`
- **Naming:** PascalCase classes, snake_case functions/methods, UPPER_SNAKE_CASE constants
- **Strings:** Single quotes preferred (`Q000` rule enabled)
- **Type annotations:** Keep existing style (`UP006`/`UP007` ignored — don't modernize annotation syntax)
- **Docstrings:** Required (D rules enabled), but `__init__` docstrings optional (`D107` ignored)
- **Max complexity:** 25 (`mccabe`)
- **Line length:** Not enforced (`E501` ignored)

## Conventions

- **wxPython GUI:** All UI uses wx dialogs/controls. Custom events defined via `wx.lib.newevent` in `events.py`.
- **Database:** SQLite3 with FTS5 for full-text part search. Project DB at `jlcpcb/project.db`, global parts DB at `jlcpcb/parts-fts5.db`.
- **KiCad API:** Accessed via `pcbnew` module. For standalone testing, use stubs from `standalone_impl.py`.
- **Bundled dependencies:** `lib/packaging` is vendored — do not add it to `pyproject.toml` dependencies.
- **Version:** Read from `VERSION` file, not hardcoded.
- **Settings:** JSON config in `settings.json`, managed by `settings.py` dialog, propagated via `EVT_UPDATE_SETTING` events.
- **`print()` allowed** in CLI modules: `db_build/jlcparts_db_convert.py`, `common/filemgr.py`, `common/partsdb.py`, `common/progress.py` (per-file `T201` ignore).

## Testing

- **Framework:** pytest with fixtures for temp directories and isolated SQLite databases
- **Pattern:** Group related tests in classes (`class TestFixDescription:`), descriptive method names
- **Mocking:** `unittest.mock` for external APIs and file I/O
- **Paths:** Use `pathlib.Path` for cross-platform compatibility
- **Test files:** Located in `common/test_*.py`
