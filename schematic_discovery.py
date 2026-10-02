"""Distinguish absent project schematics from incomplete discovery for recovery."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from uuid import UUID


@dataclass(frozen=True)
class SchematicDiscovery:
    """Root discovery evidence; present hierarchies still require full parsing."""

    paths: tuple[str, ...]
    status: str
    diagnostics: tuple[str, ...] = ()
    source_state: tuple[tuple[str, str], ...] = ()
    root_uuids: tuple[tuple[str, str], ...] = ()


def discover_project_schematics(
    project_path: Optional[str],
    board_filename: Optional[str],
    project_name: Optional[str] = None,
) -> SchematicDiscovery:
    """Read associated roots without treating unreadable metadata as absence.

    Missing project metadata is normal for a standalone PCB. A declared root
    can fall back to the existing default root, as KiCad does. Other schematic
    files in the directory never establish this board's project association.
    Content fingerprints let callers revalidate discovery before recovery.
    """
    name = project_name or os.path.splitext(os.path.basename(board_filename or ""))[0]
    if not project_path or not name:
        return SchematicDiscovery(
            (), "unresolved", ("Project identity is unavailable.",)
        )
    directory = Path(project_path).absolute()
    if not directory.is_dir():
        return SchematicDiscovery(
            (), "unresolved", (f"Project directory is unavailable: {directory}",)
        )
    states: dict[str, str] = {}

    def read(path: Path) -> Optional[bytes]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            if path.is_symlink():
                raise OSError(
                    f"Project source is a broken symbolic link: {path}"
                ) from None
            states[str(path)] = "missing"
            return None
        except OSError as error:
            states[str(path)] = f"error:{type(error).__name__}"
            raise
        states[str(path)] = hashlib.sha256(data).hexdigest()
        return data

    roots: list[str] = []
    diagnostics: list[str] = []
    declared_roots: dict[str, Optional[str]] = {}
    try:
        project = directory / f"{name}.kicad_pro"
        raw = read(project)
        listed: list[tuple[str, Optional[str]]] = []
        if raw is not None:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"Project metadata is not an object: {project}")
            schematic = data.get("schematic", {})
            if not isinstance(schematic, dict):
                raise ValueError(f"Invalid schematic project metadata: {project}")
            sheets = schematic.get("top_level_sheets", [])
            if not isinstance(sheets, list):
                raise ValueError(f"Invalid top-level sheet list: {project}")
            for sheet in sheets:
                if (
                    not isinstance(sheet, dict)
                    or not isinstance(sheet.get("filename"), str)
                    or not sheet["filename"].strip()
                ):
                    raise ValueError(f"Invalid top-level sheet entry: {project}")
                declared_uuid = sheet.get("uuid")
                if declared_uuid is not None:
                    if not isinstance(declared_uuid, str):
                        raise ValueError(f"Invalid top-level root UUID: {project}")
                    parsed_uuid = UUID(declared_uuid)
                    declared_uuid = str(parsed_uuid) if parsed_uuid.int else None
                listed.append((sheet["filename"], declared_uuid))
        default = directory / f"{name}.kicad_sch"
        for filename, declared_uuid in listed or [(default.name, None)]:
            path = Path(os.path.abspath(directory / filename))
            content = read(path)
            if content is None and path != default:
                path = default
                content = read(path)
            if content is None:
                if listed:
                    diagnostics.append(
                        f"Declared schematic root is missing: {filename}"
                    )
                continue
            key = str(path)
            if key in declared_roots and declared_roots[key] != declared_uuid:
                raise ValueError(f"Conflicting declared root UUIDs for {path}")
            declared_roots[key] = declared_uuid
            if str(path) not in roots:
                roots.append(str(path))
    except (OSError, ValueError, UnicodeError) as error:
        diagnostics.append(f"Unable to inspect project schematics: {error}")
    status = "unresolved" if diagnostics else "present" if roots else "absent"
    return SchematicDiscovery(
        tuple(roots),
        status,
        tuple(diagnostics),
        tuple(sorted(states.items())),
        tuple(
            (path, uuid) for path, uuid in declared_roots.items() if uuid is not None
        ),
    )
