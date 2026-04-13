"""Helpers for invoking kicad-cli across platforms."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def resolve_kicad_cli_path(pcbnew_module: Any = None) -> str | None:
    """Resolve the `kicad-cli` executable path across platforms.

    Resolution order:
    1. `KICAD_CLI` env var (full path)
    2. `kicad-cli` on `PATH`
    3. Platform-specific default install locations
    4. Derive app bundle path from `pcbnew` module location (macOS)
    """
    env_cli = os.getenv("KICAD_CLI", "").strip()
    if env_cli and os.path.isfile(env_cli):
        return env_cli

    if cli := shutil.which("kicad-cli"):
        return cli

    candidates: list[str] = []

    if sys.platform.startswith("win"):
        base_path = os.environ.get("KICAD_PATH", r"C:\Program Files\KiCad")
        candidates.extend(
            [
                os.path.join(base_path, "bin", "kicad-cli.exe"),
                os.path.join(base_path, "10.0", "bin", "kicad-cli.exe"),
                os.path.join(base_path, "9.0", "bin", "kicad-cli.exe"),
                os.path.join(base_path, "8.0", "bin", "kicad-cli.exe"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/Applications/KiCad/KiCad Nightly.app/Contents/MacOS/kicad-cli",
            ]
        )
    else:
        # Linux and other POSIX paths
        candidates.extend(["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"])

    pcbnew_file = getattr(pcbnew_module, "__file__", "")
    if isinstance(pcbnew_file, str) and "/Contents/" in pcbnew_file:
        contents_idx = pcbnew_file.find("/Contents/")
        if contents_idx > 0:
            app_contents = pcbnew_file[: contents_idx + len("/Contents")]
            candidates.append(os.path.join(app_contents, "MacOS", "kicad-cli"))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    return None


def run_drc(
    kicad_cli_path: str,
    board_filename: str,
    output_path: str,
) -> subprocess.CompletedProcess[str]:
    """Run `kicad-cli pcb drc` and return process result."""
    cmd = [
        kicad_cli_path,
        "pcb",
        "drc",
        board_filename,
        f"--output={output_path}",
        "--format=json",
        "--refill-zones",
        "--severity-error",
    ]
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )


def count_drc_violations(report_path: str | Path) -> int:
    """Count DRC violations in a KiCad JSON DRC report."""
    with open(report_path, encoding="utf-8") as report_file:
        report = json.load(report_file)
    return len(report.get("violations", []))


class DRCViolationCounter:
    """Run KiCad CLI DRC and return the count of error-severity violations."""

    def __init__(
        self,
        pcbnew_module: Any = None,
        working_dir: str | None = None,
    ):
        self._pcbnew_module = pcbnew_module
        self._working_dir = working_dir

    def get_violation_count(self, board_filename: str) -> int:
        """Run DRC and return violation count, raising RuntimeError on failure."""
        cli_path = resolve_kicad_cli_path(self._pcbnew_module)
        if not cli_path:
            raise RuntimeError(
                "Could not locate kicad-cli. Install KiCad CLI support, add it to PATH, "
                "or set KICAD_CLI to the executable path."
            )

        report_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                dir=self._working_dir,
            ) as report_file:
                report_path = report_file.name

            completed = run_drc(cli_path, board_filename, report_path)

            if os.path.exists(report_path) and os.path.getsize(report_path) > 0:
                return count_drc_violations(report_path)

            if completed.returncode != 0:
                msg = completed.stderr.strip() or completed.stdout.strip() or "Unknown error"
                raise RuntimeError(f"kicad-cli returned: {msg}")

            return 0
        finally:
            if report_path and os.path.exists(report_path):
                with contextlib.suppress(OSError):
                    os.remove(report_path)
