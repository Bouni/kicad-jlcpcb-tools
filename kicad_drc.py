"""Helpers for running DRC via KiCad SWIG bindings."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


def _load_board_from_path(pcbnew_module: Any, board_filename: str):
    """Load or reuse a board object suitable for `WriteDRCReport`."""
    if hasattr(pcbnew_module, "GetBoard"):
        board = pcbnew_module.GetBoard()
        if (
            board is not None
            and getattr(board, "GetFileName", lambda: "")() == board_filename
        ):
            return board

    if hasattr(pcbnew_module, "LoadBoard"):
        return pcbnew_module.LoadBoard(board_filename)

    raise RuntimeError("KiCad Python module does not expose GetBoard() or LoadBoard()")


def run_drc(pcbnew_module: Any, board_filename: str, output_path: str) -> bool:
    """Run DRC using `pcbnew.WriteDRCReport` and write a text report."""
    if not hasattr(pcbnew_module, "WriteDRCReport"):
        raise RuntimeError("KiCad Python module does not expose WriteDRCReport()")

    board = _load_board_from_path(pcbnew_module, board_filename)
    logger.info(
        "Running SWIG DRC: WriteDRCReport(board=%s, report=%s, units=EDA_UNITS_MM, report_all_track_errors=False)",
        board_filename,
        output_path,
    )
    return bool(
        pcbnew_module.WriteDRCReport(
            board,
            output_path,
            pcbnew_module.EDA_UNITS_MM,
            False,
        )
    )


def parse_drc_report(report_path: str) -> tuple[int, list[str]]:
    """Parse KiCad text DRC report and return error count plus error messages."""
    with open(report_path, encoding="utf-8") as report_file:
        report_text = report_file.read()

    section_match = re.search(
        r"\*\* Found \d+ DRC violations \*\*(.*?)(?:\n\*\* Found \d+ unconnected pads \*\*|\n\*\* End of Report \*\*)",
        report_text,
        flags=re.DOTALL,
    )
    violations_section = section_match.group(1) if section_match else report_text

    header_matches = list(
        re.finditer(r"(?m)^\[(?P<code>[^\]]+)\]:\s*(?P<message>.*)$", violations_section)
    )

    if header_matches:
        error_messages: list[str] = []
        warning_count = 0
        unknown_count = 0

        for index, header in enumerate(header_matches):
            start = header.start()
            end = (
                header_matches[index + 1].start()
                if index + 1 < len(header_matches)
                else len(violations_section)
            )
            block = violations_section[start:end]
            severity_match = re.search(r";\s*(error|warning)\b", block, flags=re.IGNORECASE)

            if not severity_match:
                unknown_count += 1
                continue

            severity = severity_match.group(1).lower()
            if severity == "error":
                code = header.group("code").strip()
                message = header.group("message").strip()
                error_messages.append(f"[{code}]: {message}")
            elif severity == "warning":
                warning_count += 1

        logger.info(
            "Parsed %d DRC violation blocks; %d error-severity, %d warning-severity, %d unknown-severity",
            len(header_matches),
            len(error_messages),
            warning_count,
            unknown_count,
        )
        return len(error_messages), error_messages

    match = re.search(r"\*\* Found (\d+) DRC violations \*\*", report_text)
    if not match:
        raise RuntimeError("Could not parse DRC violation count from report")

    logger.warning(
        "DRC report did not contain per-item severity fields; falling back to total violations count"
    )

    return int(match.group(1)), []


class DRCViolationCounter:
    """Run KiCad SWIG DRC and return the count of violations."""

    def __init__(
        self,
        pcbnew_module: Any = None,
        working_dir: str | None = None,
    ):
        self._pcbnew_module = pcbnew_module
        self._working_dir = working_dir

    def get_violation_count(self, board_filename: str) -> int:
        """Run DRC and return violation count, raising RuntimeError on failure."""
        if self._pcbnew_module is None:
            raise RuntimeError(
                "No KiCad Python module provided. Cannot run SWIG DRC without pcbnew."
            )

        report_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".rpt",
                delete=False,
                dir=self._working_dir,
            ) as report_file:
                report_path = report_file.name

            success = run_drc(self._pcbnew_module, board_filename, report_path)

            if not success:
                raise RuntimeError("WriteDRCReport returned failure")

            if os.path.exists(report_path) and os.path.getsize(report_path) > 0:
                try:
                    error_count, error_messages = parse_drc_report(report_path)
                    if error_messages:
                        logger.warning("First %d DRC error(s):", min(10, len(error_messages)))
                        for message in error_messages[:10]:
                            logger.warning("  %s", message)
                    return error_count
                except Exception as exc:
                    raise RuntimeError("Failed to parse DRC report") from exc

            if os.path.exists(report_path):
                raise RuntimeError("DRC report file was created but empty")

            return 0
        finally:
            if report_path and os.path.exists(report_path):
                with contextlib.suppress(OSError):
                    os.remove(report_path)
