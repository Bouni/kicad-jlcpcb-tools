"""Persist migration provenance independently of transient schematic-save reports.

The planner supplies a table-generation token captured with its read-only source
snapshot. This module never opens SQLite or writes a PCB. Reports retain every
startup disposition, including routine decisions, after the source table is
archived. Only the caller's explicit interactive acknowledgment suppresses a
pending generation; persisting or loading a report never does so.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from .legacy_part_migration import LegacyMigrationPlan


_REPORT_FILENAME = "legacy-migration-report.json"
_ACK_FILENAME = "legacy-migration-acknowledgments.json"
_VERSION = 1


@dataclass(frozen=True)
class LegacyMigrationReport:
    """A durable generation's human-readable override messages and notice state."""

    path: Path
    generation_id: str
    needs_acknowledgment: bool
    override_messages: tuple[str, ...]


def _directory(project_path: Union[str, Path]) -> Path:
    """Keep history and acknowledgment inside the portable project directory."""
    return Path(project_path) / "jlcpcb"


def _read_json(path: Path, collection: str) -> dict[str, Any]:
    """Reject unsupported or damaged history instead of silently replacing it."""
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        return {"version": _VERSION, collection: []}
    if (
        not isinstance(data, dict)
        or data.get("version") != _VERSION
        or not isinstance(data.get(collection), list)
    ):
        raise ValueError(f"Invalid legacy migration history: {path.name}")
    return data


def _read_acknowledgments(directory: Path) -> dict[str, Any]:
    """Read acknowledgment state without treating malformed data as delivered."""
    data = _read_json(directory / _ACK_FILENAME, "generation_ids")
    values = data["generation_ids"]
    if any(not isinstance(value, str) or not value for value in values) or len(
        set(values)
    ) != len(values):
        raise ValueError("Invalid legacy migration acknowledgment generations")
    receipts = data.setdefault("message_receipts", {})
    if not isinstance(receipts, dict) or any(
        not isinstance(key, str)
        or not isinstance(fingerprints, list)
        or any(not isinstance(value, str) for value in fingerprints)
        for key, fingerprints in receipts.items()
    ):
        raise ValueError("Invalid legacy migration acknowledgment receipts")
    return data


def _read_report(directory: Path) -> dict[str, Any]:
    """Validate every stored row needed to preserve prior audit provenance."""
    data = _read_json(directory / _REPORT_FILENAME, "generations")
    seen: set[str] = set()
    for generation in data["generations"]:
        if not isinstance(generation, dict):
            # Invalid decoded file contents are a value error to callers.
            raise ValueError("Invalid legacy migration report generation")  # noqa: TRY004
        identifier = generation.get("generation_id")
        digests = generation.get("source_digests")
        observations = generation.get("observations")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or not isinstance(generation.get("database"), str)
            or not isinstance(digests, list)
            or any(not isinstance(digest, str) for digest in digests)
            or not isinstance(observations, list)
            or not observations
        ):
            raise ValueError("Invalid legacy migration report generation")
        seen.add(identifier)
        for observation in observations:
            if (
                not isinstance(observation, dict)
                or not isinstance(observation.get("rows"), list)
                or not isinstance(observation.get("board_name", ""), str)
            ):
                raise ValueError("Invalid legacy migration report observation")  # noqa: TRY004
            for row in observation["rows"]:
                if not isinstance(row, dict) or any(
                    not isinstance(row.get(key), str)
                    for key in (
                        "reference",
                        "legacy_value",
                        "status",
                        "disposition",
                        "reason",
                    )
                ):
                    raise ValueError("Invalid legacy migration report row")
                native = row.get("native_value")
                identities = row.get("component_ids")
                if (
                    (native is not None and not isinstance(native, str))
                    or not isinstance(identities, list)
                    or any(not isinstance(value, str) for value in identities)
                    or not isinstance(row.get("identity"), list)
                ):
                    raise ValueError("Invalid legacy migration report row identity")
    return data


@contextmanager
def _report_lock(directory: Path) -> Iterator[None]:
    """Serialize project-wide audit updates; OS locks release even after a crash."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".legacy-migration-report.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            # Windows locks a byte range, including for an otherwise empty file.
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _sync_directory(path: Path) -> None:
    """Confirm a replaced report's directory entry on platforms supporting it."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_write_json(
    path: Path,
    data: dict[str, Any],
    *,
    acknowledgment: bool = False,
) -> None:
    """Replace complete UTF-8 history atomically and clean up failed temp writes."""
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        try:
            _sync_directory(path.parent)
        except OSError as error:
            if not acknowledgment:
                raise
            # The user's explicit acknowledgment already committed. Reporting a
            # retryable failure would falsely promise their notice is pending.
            # Audit writes still propagate failure to prevent source archival.
            logging.getLogger(__name__).warning(
                "Legacy migration acknowledgment committed, but directory "
                "synchronization failed for %s: %s",
                path,
                error,
            )
    finally:
        Path(temporary).unlink(missing_ok=True)


def _portable_database(project_path: Union[str, Path], dbfile: str) -> str:
    """Record a relative origin instead of embedding one machine's absolute path."""
    database = Path(dbfile)
    try:
        return database.resolve().relative_to(Path(project_path).resolve()).as_posix()
    except ValueError:
        return database.name


def _json_identity(values: Iterable[Any]) -> list[Any]:
    """Preserve SQLite BLOB identities without making routine audits unwritable."""
    return [
        {"bytes_hex": value.hex()} if isinstance(value, bytes) else value
        for value in values
    ]


def _snapshot_rows(
    plan: LegacyMigrationPlan, imported_assignments: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Capture initial native values and confirm imports only after native success."""
    rows = []
    for row in plan.rows:
        disposition = row.disposition
        native_value = row.native_value
        if (
            disposition == "pending"
            and row.component_ids
            and all(
                imported_assignments.get(key) == row.lcsc for key in row.component_ids
            )
        ):
            disposition = "imported"
            native_value = row.lcsc
        rows.append(
            {
                "reference": row.reference,
                "legacy_value": row.lcsc,
                "native_value": native_value,
                "status": row.status,
                "disposition": disposition,
                "component_ids": list(row.component_ids),
                "identity": _json_identity(row.identity),
                "reason": row.reason,
            }
        )
    return rows


def _row_identity(row: dict[str, Any], board_name: str) -> str:
    """Recognize one original source row despite later changes to native intent."""
    payload = json.dumps(
        [
            board_name,
            row["identity"],
            row["legacy_value"],
            sorted(row["component_ids"]),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _override_messages(generation: dict[str, Any]) -> tuple[str, ...]:
    """Explain startup supersessions without relabeling imported values on reopen."""
    first_rows: dict[str, tuple[str, dict[str, Any]]] = {}
    for observation in generation["observations"]:
        board_name = observation.get("board_name", "")
        for row in observation["rows"]:
            first_rows.setdefault(_row_identity(row, board_name), (board_name, row))
    messages = []
    for board_name, row in first_rows.values():
        disposition = row["disposition"]
        legacy = row["legacy_value"]
        native = row["native_value"]
        if disposition == "explicit_clear" and native == "":
            replacement = "an explicitly empty native field"
        elif (
            disposition == "board_override"
            and native
            and legacy.strip().upper() != native.strip().upper()
        ):
            replacement = f"native value {native}"
        else:
            continue
        identities = ", ".join(row["component_ids"])
        origin = f"{board_name}: " if board_name else ""
        messages.append(
            f"{origin}{row['reference']} [{identities}]: legacy {legacy} was superseded by "
            f"{replacement}; {row['reason']}."
        )
    return tuple(dict.fromkeys(messages))


def _message_receipt(message: str) -> str:
    """Identify exactly one disclosed decision without storing it twice."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _result(
    directory: Path, generation: dict[str, Any], acknowledgments: dict[str, Any]
) -> LegacyMigrationReport:
    """Return a report without acknowledging or mutating any application state."""
    identifier = generation["generation_id"]
    receipts = acknowledgments["message_receipts"].get(identifier, ())
    messages = tuple(
        message
        for message in _override_messages(generation)
        if _message_receipt(message) not in receipts
    )
    return LegacyMigrationReport(
        directory / _REPORT_FILENAME,
        identifier,
        identifier not in acknowledgments["generation_ids"] or bool(messages),
        messages,
    )


def record_legacy_migration_report(
    project_path: Union[str, Path],
    dbfile: str,
    plan: LegacyMigrationPlan,
    *,
    imported_assignments: Union[Mapping[str, str], Iterable[tuple[str, str]]] = (),
    board_name: str = "",
) -> Optional[LegacyMigrationReport]:
    """Persist all startup decisions before permitting archival of their source.

    Supply the original startup plan, with successfully applied assignments, so
    imports retain their provenance. Repeated identical observations are no-ops;
    subsequent different observations remain in the generation's history. The
    initial observation of each legacy row in each board controls messages. A
    portable board filename distinguishes copied boards with identical UUIDs.
    All I/O,
    lock, and malformed-history errors propagate for the caller to retain retry
    state and active recovery data. Inactive plans create no files.
    """
    if not plan.active_table:
        return None
    identifier = plan.active_generation
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Legacy migration report requires an active generation")
    directory = _directory(project_path)
    rows = _snapshot_rows(plan, dict(imported_assignments))
    with _report_lock(directory):
        data = _read_report(directory)
        acknowledgments = _read_acknowledgments(directory)
        original = json.dumps(data, sort_keys=True)
        generation = next(
            (
                item
                for item in data["generations"]
                if item["generation_id"] == identifier
            ),
            None,
        )
        if generation is None:
            generation = {
                "generation_id": identifier,
                "database": _portable_database(project_path, dbfile),
                "source_digests": [],
                "observations": [],
            }
            data["generations"].append(generation)
        if (
            plan.active_digest
            and plan.active_digest not in generation["source_digests"]
        ):
            generation["source_digests"].append(plan.active_digest)
        observation = {
            "board_name": board_name.replace("\\", "/").rsplit("/", 1)[-1],
            "rows": rows,
        }
        if observation not in generation["observations"]:
            generation["observations"].append(observation)
        generation["override_messages"] = list(_override_messages(generation))
        if json.dumps(data, sort_keys=True) != original:
            _atomic_write_json(directory / _REPORT_FILENAME, data)
        else:
            # A prior replacement may have succeeded before directory fsync
            # failed. Identical bytes alone cannot authorize source archival.
            _sync_directory(directory)
        return _result(directory, generation, acknowledgments)


def load_pending_legacy_migration_reports(
    project_path: Union[str, Path],
) -> tuple[LegacyMigrationReport, ...]:
    """Find notices even after archival, without creating files or acknowledging."""
    directory = _directory(project_path)
    data = _read_report(directory)
    acknowledgments = _read_acknowledgments(directory)
    return tuple(
        report
        for generation in data["generations"]
        for report in (_result(directory, generation, acknowledgments),)
        if report.needs_acknowledgment
    )


def acknowledge_legacy_migration_report(
    project_path: Union[str, Path],
    generation_id: str,
    *,
    messages: Optional[Iterable[str]] = None,
) -> None:
    """Persist an explicit interactive acknowledgment separately from audit data.

    A forced close or cancelled dialog must not call this function. A failure
    before atomic replacement leaves the report pending; a later directory-sync
    failure is logged as a committed write. Pass the exact messages displayed to
    avoid acknowledging another window's concurrent, unseen decisions. Omitting
    messages acknowledges every decision currently persisted in the generation.
    No acknowledgment can persist a PCB.
    """
    directory = _directory(project_path)
    with _report_lock(directory):
        data = _read_report(directory)
        generation = next(
            (
                item
                for item in data["generations"]
                if item["generation_id"] == generation_id
            ),
            None,
        )
        if generation is None:
            raise ValueError("Legacy migration generation has no persisted report")
        recorded_messages = _override_messages(generation)
        displayed = tuple(recorded_messages if messages is None else messages)
        if any(message not in recorded_messages for message in displayed):
            raise ValueError("Legacy migration acknowledgment has no persisted message")
        acknowledgments = _read_acknowledgments(directory)
        original = json.dumps(acknowledgments, sort_keys=True)
        if generation_id not in acknowledgments["generation_ids"]:
            acknowledgments["generation_ids"].append(generation_id)
        receipts = acknowledgments["message_receipts"].setdefault(generation_id, [])
        for message in displayed:
            fingerprint = _message_receipt(message)
            if fingerprint not in receipts:
                receipts.append(fingerprint)
        if json.dumps(acknowledgments, sort_keys=True) != original:
            _atomic_write_json(
                directory / _ACK_FILENAME, acknowledgments, acknowledgment=True
            )
