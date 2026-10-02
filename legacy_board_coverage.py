"""Prove active legacy recovery is no longer needed by any saved sibling PCB.

Native loading is injected and returns detached Default footprint records. This
module never saves a board, changes the live board, or reads catalog data. The
returned filesystem evidence must be checked again inside the archival lock.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Optional, Protocol

from .lcsc import is_lcsc_part
from .part_assignments import ResolvedAssignment, safe_assignment_value

LegacyIdentity = tuple[str, str, str, bool, bool]

# RFC 1740, appendices A/B: big-endian AppleDouble magic, v2, and a
# 26-byte fixed header followed by 12-byte entry descriptors.
_APPLEDOUBLE_SIGNATURE = b"\x00\x05\x16\x07\x00\x02\x00\x00"
_APPLEDOUBLE_HEADER_SIZE = 26
_APPLEDOUBLE_ENTRY_SIZE = 12


class MigrationPart(Protocol):
    """Shared structural contract for live and detached saved Default values."""

    component_id: str
    reference: str
    value: str
    footprint: str
    bom: bool
    pos: bool
    assignment: ResolvedAssignment
    lcsc: str


class LegacyCoverageRow(Protocol):
    """Planner decision needed to verify current and shared-directory ownership."""

    reference: str
    lcsc: str
    status: str
    component_ids: tuple[str, ...]
    reason: str
    identity: LegacyIdentity
    native_value: Optional[str]


@dataclass(frozen=True)
class SavedBoardPart:
    """Immutable native identity, matching tuple, and resolved field evidence."""

    component_id: str
    reference: str
    value: str
    footprint: str
    bom: bool
    pos: bool
    assignment: ResolvedAssignment
    lcsc: str

    @property
    def identity(self) -> LegacyIdentity:
        """Match the historical reference/value/itemname/BOM/POS tuple exactly."""
        return self.reference, self.value, self.footprint, not self.bom, not self.pos

    @property
    def native_value(self) -> Optional[str]:
        """Return only an unambiguous native ID or explicit clear."""
        return safe_assignment_value(self.assignment, self.lcsc)


@dataclass(frozen=True)
class BoardSourceToken:
    """Content and physical file identity, retaining the observed directory name."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str

    @property
    def physical_id(self) -> tuple[int, int, str]:
        """Identify physical aliases, keeping paths when inode IDs are unavailable."""
        return (
            self.device,
            self.inode,
            "" if self.inode else os.path.realpath(self.path),
        )


@dataclass(frozen=True)
class SavedBoardSnapshot:
    """One unique physical PCB loaded between matching source fingerprints."""

    path: str
    parts: tuple[SavedBoardPart, ...]
    token: BoardSourceToken


@dataclass(frozen=True)
class SavedBoardCoverage:
    """Archival eligibility and the source evidence needed to recheck it."""

    eligible: bool
    diagnostics: tuple[str, ...]
    snapshots: tuple[SavedBoardSnapshot, ...]
    source_tokens: tuple[BoardSourceToken, ...]
    directory: str
    candidate_paths: tuple[str, ...]
    current_path: str
    advisories: tuple[str, ...] = ()
    requires_sources: bool = True
    # Portable sibling notices support durable disclosure without treating
    # current-board save advice as a new migration notification.
    sibling_notices: tuple[str, ...] = ()
    # Exact diagnostic subset lets callers route sibling notices separately,
    # while preserving every diagnostic as an archival blocker.
    sibling_diagnostics: tuple[str, ...] = ()


def _is_appledouble_sidecar(path: str) -> bool:
    """Recognize stable, complete v2 metadata headers under the ``._`` convention.

    A filename alone is never sufficient: real PCBs can use the same prefix.
    Unsupported, truncated, unreadable, or concurrently replaced headers remain
    saved-board candidates. Filler bytes vary across macOS versions and are not
    part of the format signature. Metadata entry payloads are not parsed here.
    """
    if not os.path.basename(path).startswith("._"):
        return False
    try:
        with open(path, "rb") as source:
            before = os.fstat(source.fileno())
            header = source.read(_APPLEDOUBLE_HEADER_SIZE)
            after = os.fstat(source.fileno())
        return (
            len(header) == _APPLEDOUBLE_HEADER_SIZE
            and header.startswith(_APPLEDOUBLE_SIGNATURE)
            and before.st_size
            >= _APPLEDOUBLE_HEADER_SIZE
            + int.from_bytes(header[24:26], "big") * _APPLEDOUBLE_ENTRY_SIZE
            and _stat_identity(before)
            == _stat_identity(after)
            == _stat_identity(os.stat(path))
        )
    except OSError:
        return False


def _candidate_paths(directory: str, current_path: str) -> tuple[str, ...]:
    """Include current/direct PCB siblings, excluding proven metadata sidecars."""
    paths = {current_path}
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.casefold().endswith(".kicad_pcb"):
                continue
            path = os.path.join(directory, entry.name)
            if path == current_path or not _is_appledouble_sidecar(path):
                paths.add(path)
    return tuple(sorted(paths))


def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    """Detect same-byte replacement as well as ordinary saves and truncation."""
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _source_token(path: str) -> BoardSourceToken:
    """Fingerprint a stable read and reject replacement during that read."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if not (
        _stat_identity(before) == _stat_identity(after) == _stat_identity(os.stat(path))
    ):
        raise OSError(f"PCB changed while reading {path}")
    return BoardSourceToken(path, *_stat_identity(after), digest.hexdigest())


def _capture_parts(parts: Sequence[MigrationPart]) -> tuple[SavedBoardPart, ...]:
    """Detach values and fail closed on malformed or duplicate footprint UUIDs."""
    result = []
    identities: set[str] = set()
    for part in parts:
        if not all(
            isinstance(value, str)
            for value in (
                part.component_id,
                part.reference,
                part.value,
                part.footprint,
                part.lcsc,
            )
        ):
            raise ValueError("Saved PCB identities and assignment values must be text")
        if not part.component_id or part.component_id in identities:
            raise ValueError("Saved PCB requires unique nonempty footprint UUIDs")
        if type(part.bom) is not bool or type(part.pos) is not bool:
            raise ValueError("Saved PCB requires boolean BOM and POS states")
        captured = SavedBoardPart(
            part.component_id,
            part.reference,
            part.value,
            part.footprint,
            part.bom,
            part.pos,
            part.assignment,
            part.lcsc,
        )
        # Validate alias provenance during capture, even for unrelated footprints.
        captured.native_value
        result.append(captured)
        identities.add(part.component_id)
    return tuple(result)


def _row_diagnostics(
    rows: Sequence[LegacyCoverageRow],
    current_parts: tuple[SavedBoardPart, ...],
    snapshots: tuple[SavedBoardSnapshot, ...],
    current_path: str,
    schematic_saved_ids: frozenset[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Protect recoverable saved rows and prove each chosen current assignment."""
    diagnostics = []
    advisories = []
    sibling_notices = []
    sibling_diagnostics = []
    current_by_id = {part.component_id: part for part in current_parts}
    current_snapshot = next(
        (snapshot for snapshot in snapshots if snapshot.path == current_path), None
    )
    saved_by_id = (
        {part.component_id: part for part in current_snapshot.parts}
        if current_snapshot is not None
        else {}
    )
    for row in rows:
        if row.status == "ignored":
            continue
        label = f"{row.reference or '(unannotated)'} legacy {row.lcsc}"
        if row.status == "unresolved":
            diagnostics.append(f"{label}: recovery is unresolved ({row.reason}).")
            continue
        identity = row.identity
        if (
            row.status not in {"planned", "accounted", "obsolete"}
            or len(identity) != 5
            or not all(isinstance(value, str) for value in identity[:3])
            or not all(type(value) is bool for value in identity[3:])
            or not is_lcsc_part(row.lcsc)
        ):
            diagnostics.append(f"{label}: recovery identity or decision is incomplete.")
            continue
        durable_ids: set[str] = set()
        if row.status in {"planned", "accounted"}:
            if not row.component_ids:
                diagnostics.append(f"{label}: current recovery target is missing.")
            for component_id in row.component_ids:
                current = current_by_id.get(component_id)
                if (
                    current is None
                    or current.identity != identity
                    or row.native_value is None
                    or current.native_value != row.native_value
                ):
                    diagnostics.append(
                        f"{label} [{component_id}]: planned native choice is not "
                        "present on the current board."
                    )
                    continue
                if component_id in schematic_saved_ids:
                    durable_ids.add(component_id)
                    continue
                saved = saved_by_id.get(component_id)
                if saved is not None and saved.native_value == current.native_value:
                    durable_ids.add(component_id)
                else:
                    advisories.append(
                        f"{label} [{component_id}]: save {current_path} explicitly "
                        "before retiring recovery; its saved assignment does not "
                        "match the current native choice."
                    )
        for snapshot in snapshots:
            for part in snapshot.parts:
                if part.identity != identity:
                    continue
                if snapshot.path == current_path and part.component_id in durable_ids:
                    continue
                if part.native_value is None:
                    missing = (
                        part.assignment.status == "missing"
                        and not part.assignment.aliases
                    )
                    message = (
                        f"{label} [{part.component_id}]: {snapshot.path} still needs "
                        "active recovery; its saved native assignment is missing "
                        "or unsafe."
                    )
                    (advisories if missing else diagnostics).append(message)
                    if snapshot.path != current_path:
                        sibling_notices.append(
                            f"{os.path.basename(snapshot.path)}: {label} "
                            f"[{part.component_id}] still needs active recovery; its "
                            "saved native assignment is "
                            + ("missing." if missing else "unsafe.")
                        )
                        if not missing:
                            sibling_diagnostics.append(message)
    return diagnostics, advisories, sibling_notices, sibling_diagnostics


def collect_saved_board_coverage(
    current_path: str,
    current_footprints: Sequence[MigrationPart],
    rows: Sequence[LegacyCoverageRow],
    *,
    load_board: Callable[[str], Sequence[MigrationPart]],
    schematic_saved_ids: frozenset[str] = frozenset(),
) -> SavedBoardCoverage:
    """Read all saved ownership evidence without implicitly saving any PCB.

    ``load_board`` must load detached saved data and capture Default values; it
    must never return the live unsaved board. The caller invokes this after native
    recovery and any schematic save. Only successfully saved schematic UUIDs may
    be supplied as ``schematic_saved_ids``. Obsolete rows still require a scan of
    saved boards because discarding unsaved tuple changes can make them useful.
    Empty tables or wholly invalid legacy IDs have no recoverable assignments,
    so they explicitly need no saved-board evidence.
    """
    if all(row.status == "ignored" and not is_lcsc_part(row.lcsc) for row in rows):
        return SavedBoardCoverage(
            eligible=True,
            diagnostics=(),
            snapshots=(),
            source_tokens=(),
            directory="",
            candidate_paths=(),
            current_path=current_path,
            requires_sources=False,
        )
    supplied_path = current_path
    current_path = os.path.abspath(current_path)
    directory = str(Path(current_path).parent)
    diagnostics: list[str] = []
    sibling_notices: list[str] = []
    sibling_diagnostics: list[str] = []
    snapshots: list[SavedBoardSnapshot] = []
    tokens: list[BoardSourceToken] = []
    candidates: tuple[str, ...] = ()
    current_parts: tuple[SavedBoardPart, ...] = ()
    try:
        if not supplied_path:
            raise ValueError("Current PCB has no saved filename")
        current_parts = _capture_parts(current_footprints)
        candidates = _candidate_paths(directory, current_path)
    except Exception as error:
        diagnostics.append(
            f"Cannot read saved PCB ownership for {current_path}: {error}"
        )
    physical: dict[tuple[int, int, str], SavedBoardSnapshot] = {}
    # Prefer the actual current pathname so its physical aliases cannot make it
    # appear to be another board needing separate recovery.
    for path in sorted(
        candidates, key=lambda candidate: (candidate != current_path, candidate)
    ):
        try:
            before = _source_token(path)
            if before.physical_id not in physical:
                parts = _capture_parts(load_board(path))
                if _source_token(path) != before:
                    raise OSError("PCB changed while loading its saved values")
                snapshot = SavedBoardSnapshot(path, parts, before)
                snapshots.append(snapshot)
                physical[before.physical_id] = snapshot
            tokens.append(before)
        except Exception as error:
            message = f"Cannot read saved PCB {path}: {error}"
            diagnostics.append(message)
            if path != current_path:
                # Loader exceptions can embed the supplied absolute pathname.
                details = str(error).replace(path, os.path.basename(path))
                sibling_notices.append(
                    f"Cannot read saved PCB {os.path.basename(path)}: {details}"
                )
                sibling_diagnostics.append(message)
    row_diagnostics, advisories, row_notices, row_sibling_diagnostics = (
        _row_diagnostics(
            rows, current_parts, tuple(snapshots), current_path, schematic_saved_ids
        )
    )
    diagnostics.extend(row_diagnostics)
    sibling_notices.extend(row_notices)
    sibling_diagnostics.extend(row_sibling_diagnostics)
    result = SavedBoardCoverage(
        not diagnostics and not advisories,
        tuple(dict.fromkeys(diagnostics)),
        tuple(snapshots),
        tuple(tokens),
        directory,
        candidates,
        current_path,
        tuple(dict.fromkeys(advisories)),
        sibling_notices=tuple(dict.fromkeys(sibling_notices)),
        sibling_diagnostics=tuple(dict.fromkeys(sibling_diagnostics)),
    )
    if result.eligible and not verify_saved_board_sources(result):
        return SavedBoardCoverage(
            False,
            ("Saved PCB sources changed during recovery coverage inspection.",),
            result.snapshots,
            result.source_tokens,
            directory,
            candidates,
            current_path,
        )
    return result


def verify_saved_board_sources(coverage: SavedBoardCoverage) -> bool:
    """Recheck eligible evidence under the SQLite archival write lock.

    New/deleted sibling files, same-byte replacements, changed symlink targets,
    unreadable files, and changed content all retain the active recovery table.
    The caller must independently verify that the live board has not changed
    identity or filename (Save As) since it requested this coverage. Explicit
    no-recoverable-row outcomes need no PCB read; the caller still validates the
    legacy database digest and live board context before archival.
    """
    if not coverage.eligible:
        return False
    if not coverage.requires_sources:
        return not (
            coverage.diagnostics
            or coverage.advisories
            or coverage.snapshots
            or coverage.source_tokens
            or coverage.candidate_paths
            or coverage.sibling_notices
            or coverage.sibling_diagnostics
        )
    if not coverage.source_tokens:
        return False
    try:
        if (
            _candidate_paths(coverage.directory, coverage.current_path)
            != coverage.candidate_paths
        ):
            return False
        if {token.path for token in coverage.source_tokens} != set(
            coverage.candidate_paths
        ):
            return False
        if not all(
            _source_token(token.path) == token for token in coverage.source_tokens
        ):
            return False
        return (
            _candidate_paths(coverage.directory, coverage.current_path)
            == coverage.candidate_paths
        )
    except (OSError, ValueError):
        return False
