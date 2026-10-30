"""Fingerprint adjacent native rule definitions without changing project files.

KiCad's active PROJECT path is not exposed consistently by the SWIG API. Include
all directly adjacent native projects/rules conservatively, never searching
parents or children. This is review invalidation, not rule or impedance checking.
"""

import hashlib
import json
from pathlib import Path
import stat
from typing import Any, NoReturn

_KINDS = ("kicad_dru", "kicad_pro")


class ProjectContextError(RuntimeError):
    """Report native project definitions that cannot be fingerprinted safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate JSON keys instead of silently choosing a value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    """Reject non-standard numeric constants accepted by Python's JSON decoder."""
    raise ValueError(f"Non-standard JSON constant: {value}")


def _canonical(value: Any) -> str:
    """Represent JSON deterministically without accepting non-finite numbers."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _object_member(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Allow absent settings while rejecting present unfamiliar container schemas."""
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be a JSON object")
    return value


def _project_definition(contents: bytes) -> bytes:
    """Select native design/net/text settings, excluding version and view metadata."""
    document = json.loads(
        contents.decode("utf-8-sig"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(document, dict):
        raise TypeError("The project root must be a JSON object")
    board = _object_member(document, "board")
    selected = {}
    for parent, key, label in (
        (board, "design_settings", "board.design_settings"),
        (document, "net_settings", "net_settings"),
        (document, "text_variables", "text_variables"),
    ):
        value = _object_member(parent, key)
        if key == "text_variables":
            if any(not isinstance(item, str) for item in value.values()):
                raise ValueError("text_variables values must be strings")
        else:
            # Strip only the native container's own metadata. A text variable
            # or a future nested rule named "meta" remains meaningful data.
            value = {name: item for name, item in value.items() if name != "meta"}
        selected[label] = {"present": key in parent, "value": value}
    return _canonical(selected).encode("ascii")


def _definition_record(path: Path) -> str:
    """Read one adjacent regular file and return a filename-qualified content hash."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("The definition is not a regular file")
        contents = path.read_bytes()
        if path.suffix.lower() == ".kicad_pro":
            contents = _project_definition(contents)
        payload = {
            "filename": path.name,
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
        return f"project-context:{path.suffix[1:].lower()}:{_canonical(payload)}"
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ProjectContextError(
            f"Cannot read impedance review definitions in {path.name}: {error}"
        ) from error


def project_context_records(board_path: Path) -> tuple[str, ...]:
    """Hash adjacent native definitions for an absolute saved PCB filename.

    Include absent-file sentinels and filenames so creation, deletion and rename
    invalidate review. Read contents on every call, regardless of timestamps.
    Regular-file symlinks are read as adjacent definitions; broken links fail.
    """
    board_path = Path(board_path)
    if not board_path.is_absolute() or board_path.suffix.lower() != ".kicad_pcb":
        raise ProjectContextError(
            "Impedance review requires an absolute saved PCB filename."
        )
    try:
        definitions = sorted(
            (
                entry
                for entry in board_path.parent.iterdir()
                if entry.suffix.lower() in {"." + kind for kind in _KINDS}
            ),
            key=lambda entry: entry.name,
        )
    except OSError as error:
        raise ProjectContextError(
            f"Cannot read the impedance project directory {board_path.parent}: {error}"
        ) from error
    records = ["project-context:version:1"]
    for kind in _KINDS:
        paths = [path for path in definitions if path.suffix.lower() == "." + kind]
        if not paths:
            records.append(f"project-context:{kind}:absent")
        records.extend(_definition_record(path) for path in paths)
    return tuple(records)
