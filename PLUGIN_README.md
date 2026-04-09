# KiCad IPC Plugin Packaging Notes

This repository supports two runtime paths:

- SWIG Action Plugin (legacy compatibility, including KiCad 8.x)
- IPC Plugin (KiCad 9.0+)

For IPC, the plugin folder must contain `plugin.json` at the top level.
In this repository, `plugin.json` is intentionally kept at repository root so the root can be used directly as the plugin folder during local development.

## Install locations

KiCad scans IPC plugins from `${KICAD_DOCUMENTS_HOME}/<version>/plugins/`.
Typical defaults:

- macOS: `~/Documents/KiCad/<version>/plugins/`
- Linux: `~/.local/share/KiCad/<version>/plugins/`
- Windows: `C:\Users\<user>\Documents\KiCad\<version>\plugins\`

Install this plugin by placing a folder named `kicad-jlcpcb-tools` under that path, containing `plugin.json` and plugin sources.

## Development install

A simple development workflow is to symlink the repo:

- Link repo root to `${KICAD_DOCUMENTS_HOME}/<version>/plugins/kicad-jlcpcb-tools`
- Launch KiCad PCB Editor
- Wait for KiCad to finish creating the plugin venv (first launch can take time)

## Runtime and fallback behavior

- IPC runtime is preferred when launched by KiCad IPC plugin host.
- If IPC is unavailable, provider logic falls back to SWIG adapters.
- Export path keeps compatibility fallback (`kicad-cli`/SWIG based on availability and version gates).

## Debugging tips

- Check KiCad warning messages area for plugin stdout/stderr.
- Enable KiCad API tracing:
  - `KICAD_ENABLE_WXTRACE=1`
  - `WXTRACE=KICAD_API`
- Enable API logging in `kicad_advanced` config:
  - `EnableAPILogging=1`
- Plugin-managed Python environments are under `${KICAD_CACHE_HOME}/python-environments/<plugin_identifier>`.

## Build artifact expectation

`pyproject.toml` includes packaging entries so that build artifacts include:

- `plugin.json`
- `plugin_manifest/py.typed`
- plugin runtime metadata files (`VERSION`, `settings.json`, icon)
