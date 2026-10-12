"""Persist migration provenance independently of transient schematic-save reports.

The planner supplies a table-generation token captured with its read-only source
snapshot. This module never opens SQLite or writes a PCB. Reports retain every
startup disposition, including routine decisions, after the source table is
archived. Only the caller's explicit interactive acknowledgment suppresses a
pending generation; persisting or loading a report never does so.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any, Optional, Union

from .core.file_lock import file_lock
from .lcsc import normalize_lcsc

if TYPE_CHECKING:
    from .legacy_part_migration import LegacyMigrationPlan


_REPORT_FILENAME = "legacy-migration-report.json"
_ACK_FILENAME = "legacy-migration-acknowledgments.json"
_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LegacyMigrationReport:
    """A durable generation's pending provenance and recovery retention notices."""

    path: Path
    generation_id: str
    needs_acknowledgment: bool
    override_messages: tuple[str, ...]
    retention_messages: tuple[str, ...] = ()

    @property
    def messages(self) -> tuple[str, ...]:
        """Return the exact notices an interactive acknowledgment may disclose."""
        return tuple(dict.fromkeys((*self.override_messages, *self.retention_messages)))


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
        if "retired" in generation and type(generation["retired"]) is not bool:
            raise ValueError("Invalid legacy migration retired state")
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
                order = row.get("provenance_order")
                if order is not None and (type(order) is not int or order < 0):
                    raise ValueError("Invalid legacy migration provenance order")
        known_overrides = generation.get("known_override_messages", [])
        if (
            not isinstance(known_overrides, list)
            or any(
                not isinstance(message, str) or not message
                for message in known_overrides
            )
            or len(set(known_overrides)) != len(known_overrides)
        ):
            raise ValueError("Invalid legacy migration override message history")
        notices = generation.get("retention_notices", {})
        if not isinstance(notices, dict):
            raise ValueError("Invalid legacy migration retention notices")  # noqa: TRY004
        for board, notice in notices.items():
            if not isinstance(board, str) or not isinstance(notice, dict):
                raise ValueError("Invalid legacy migration retention notice")  # noqa: TRY004
            for field in ("current_messages", "known_messages"):
                messages = notice.get(field)
                if (
                    not isinstance(messages, list)
                    or any(
                        not isinstance(message, str) or not message
                        for message in messages
                    )
                    or len(set(messages)) != len(messages)
                ):
                    raise ValueError("Invalid legacy migration retention messages")
            if not set(notice["current_messages"]).issubset(notice["known_messages"]):
                raise ValueError(
                    "Legacy migration retention notice has no persisted history"
                )
    return data


@contextmanager
def _report_lock(directory: Path) -> Iterator[None]:
    """Wait briefly for project-wide writers without indefinitely blocking close."""
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(
        directory / ".legacy-migration-report.lock",
        timeout=_LOCK_TIMEOUT_SECONDS,
        timeout_message="Another KiCad window or process is saving the migration audit. Try again.",
    ):
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


def _compact_observations(
    generation: dict[str, Any],
    observation: Optional[dict[str, Any]] = None,
    *,
    provenance_only: bool = False,
    provenance_order: Optional[int] = None,
) -> None:
    """Keep first-row provenance and one latest snapshot in the existing v1 shape.

    The first observation per board contains each distinct row's original
    disposition; its optional second observation contains the latest board state.
    Older readers still derive exactly the same first-row override decisions.
    Genuine new source/target identities grow provenance, while repeat sessions
    with the same rows do not duplicate full snapshots. Delayed startup retries
    fill provenance without replacing an already observed current board state.
    An earlier ordered startup retry may correct a later ordered origin. Origins
    lacking capture order retain the conservative first-persisted interpretation.
    """
    first: dict[str, dict[str, dict[str, Any]]] = {}
    latest: dict[str, dict[str, Any]] = {}
    for item in generation["observations"]:
        board = item.get("board_name", "")
        origins = first.setdefault(board, {})
        for row in item["rows"]:
            origins.setdefault(_row_identity(row, board), row)
        latest[board] = item
    if observation is not None:
        board = observation["board_name"]
        origins = first.setdefault(board, {})
        for row in observation["rows"]:
            candidate = row
            if provenance_only and provenance_order is not None:
                candidate = dict(row, provenance_order=provenance_order)
            identity = _row_identity(row, board)
            original = origins.get(identity)
            if original is None or (
                (old_order := original.get("provenance_order")) is not None
                and (new_order := candidate.get("provenance_order")) is not None
                and new_order < old_order
            ):
                origins[identity] = candidate
        if not provenance_only or board not in latest:
            latest[board] = observation
    observations = []
    for board, origins in first.items():
        provenance = {"board_name": board, "rows": list(origins.values())}
        observations.append(provenance)
        current = latest[board]
        if current["rows"] != provenance["rows"]:
            observations.append(dict(current, board_name=board))
    generation["observations"] = observations


def _retention_messages(
    generation: dict[str, Any], *, current: bool = True
) -> tuple[str, ...]:
    """Read current blockers or the small ledger used to validate open dialogs."""
    if current and generation.get("retired", False):
        return ()
    field = "current_messages" if current else "known_messages"
    return tuple(
        dict.fromkeys(
            message
            for notice in generation.get("retention_notices", {}).values()
            for message in notice[field]
        )
    )


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
            and normalize_lcsc(legacy) != normalize_lcsc(native)
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
    overrides = tuple(
        message
        for message in _override_messages(generation)
        if _message_receipt(message) not in receipts
    )
    retention = tuple(
        message
        for message in _retention_messages(generation)
        if _message_receipt(message) not in receipts
    )
    return LegacyMigrationReport(
        directory / _REPORT_FILENAME,
        identifier,
        identifier not in acknowledgments["generation_ids"]
        or bool(overrides or retention),
        overrides,
        retention,
    )


def record_legacy_migration_report(
    project_path: Union[str, Path],
    dbfile: str,
    plan: LegacyMigrationPlan,
    *,
    imported_assignments: Union[Mapping[str, str], Iterable[tuple[str, str]]] = (),
    board_name: str = "",
    provenance_only: bool = False,
    provenance_order: Optional[int] = None,
    retention_messages: Optional[Iterable[str]] = None,
) -> Optional[LegacyMigrationReport]:
    """Persist all startup decisions before permitting archival of their source.

    Supply the original startup plan, with successfully applied assignments, so
    imports retain their provenance. First-row provenance and the latest snapshot
    stay separate; repeated identical observations are no-ops. Pass
    ``provenance_only=True`` for startup or delayed startup retries so they cannot
    replace newer rows or blockers. An optional startup ``provenance_order``
    captured before reading/applying the startup plan allows an earlier retry to
    correct later ordered attribution; legacy origins without order remain
    first-persisted. Epoch-clock changes or differing machine clocks can limit
    attribution ordering. These values never authorize archival. Fresh
    finalization may replace this board's current retention messages; ``None``
    leaves them unchanged. Messages must already use portable paths and stable
    wording. Previously persisted retention
    messages and replaced override messages remain in small ledgers so an open
    dialog can acknowledge exactly what it displayed after a blocker changes or
    resolves. Acknowledgment never grants archival eligibility.
    Terminal notice resolution requires
    ``mark_legacy_migration_reports_retired`` after actual archival or a rechecked
    inactive source. Retired generations accept provenance retries but ignore
    stale current retention messages.
    All I/O, lock, and malformed-history errors propagate for the caller to
    retain retry state and active recovery data.
    Inactive plans create no files.
    """
    if not plan.active_table:
        return None
    identifier = plan.active_generation
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("Legacy migration report requires an active generation")
    if provenance_order is not None and (
        type(provenance_order) is not int or provenance_order < 0
    ):
        raise ValueError(
            "Legacy migration provenance order must be a nonnegative integer"
        )
    directory = _directory(project_path)
    rows = _snapshot_rows(plan, dict(imported_assignments))
    current_messages = None
    if retention_messages is not None and not provenance_only:
        supplied_messages = tuple(retention_messages)
        if any(
            not isinstance(message, str) or not message for message in supplied_messages
        ):
            raise ValueError(
                "Legacy migration retention messages must be nonempty text"
            )
        current_messages = list(dict.fromkeys(supplied_messages))
    with _report_lock(directory):
        data = _read_report(directory)
        acknowledgments = _read_acknowledgments(directory)
        original = json.dumps(data, sort_keys=True)
        for item in data["generations"]:
            _compact_observations(item)
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
        previous_overrides = _override_messages(generation)
        if (
            plan.active_digest
            and plan.active_digest not in generation["source_digests"]
        ):
            generation["source_digests"].append(plan.active_digest)
        observation = {
            "board_name": board_name.replace("\\", "/").rsplit("/", 1)[-1],
            "rows": rows,
        }
        _compact_observations(
            generation,
            observation,
            provenance_only=provenance_only,
            provenance_order=provenance_order,
        )
        retired = generation.get("retired", False)
        if current_messages is not None and not retired:
            notice = generation.setdefault("retention_notices", {}).setdefault(
                observation["board_name"],
                {"current_messages": [], "known_messages": []},
            )
            notice["current_messages"] = current_messages
            for message in current_messages:
                if message not in notice["known_messages"]:
                    notice["known_messages"].append(message)
        generation["override_messages"] = list(_override_messages(generation))
        generation["known_override_messages"] = list(
            dict.fromkeys(
                (
                    *generation.get("known_override_messages", ()),
                    *previous_overrides,
                    *generation["override_messages"],
                )
            )
        )
        if json.dumps(data, sort_keys=True) != original:
            _atomic_write_json(directory / _REPORT_FILENAME, data)
        else:
            # A prior replacement may have succeeded before directory fsync
            # failed. Identical bytes alone cannot authorize source archival.
            _sync_directory(directory)
        return _result(directory, generation, acknowledgments)


def mark_legacy_migration_reports_retired(
    project_path: Union[str, Path],
    dbfile: str,
    *,
    generation_id: Optional[str] = None,
    source_check: Optional[Callable[[], bool]] = None,
) -> None:
    """Resolve current blockers only after the source is confirmed retired.

    After a successful table rename, supply its exact generation. An inactive
    planner may reconcile every generation for the same portable database, but
    must supply ``source_check`` to recheck inactivity under the report lock.
    A false result is a no-op; check failures propagate without changing history.
    This module never reads SQLite or derives retirement from advisory wording.
    Preserve startup provenance and known messages for pending dialogs. A retired
    generation cannot regain current blockers from a stale active-plan writer.
    A failed marker write can be retried after the next confirmed inactive read.
    Projects with no report create no directory, file, or lock sidecar.
    """
    if generation_id is not None and (
        not isinstance(generation_id, str) or not generation_id
    ):
        raise ValueError("Legacy migration retirement requires a valid generation")
    if generation_id is None and source_check is None:
        raise ValueError(
            "Legacy migration retirement requires a generation or source_check"
        )
    directory = _directory(project_path)
    path = directory / _REPORT_FILENAME
    if not path.exists():
        return
    database = _portable_database(project_path, dbfile)
    with _report_lock(directory):
        data = _read_report(directory)
        generations = [
            item
            for item in data["generations"]
            if item["database"] == database
            and (generation_id is None or item["generation_id"] == generation_id)
        ]
        if not generations or (source_check is not None and not source_check()):
            return
        original = json.dumps(data, sort_keys=True)
        for generation in generations:
            generation["retired"] = True
            for notice in generation.get("retention_notices", {}).values():
                notice["current_messages"] = []
        if json.dumps(data, sort_keys=True) != original:
            _atomic_write_json(path, data)
        else:
            # A replacement may have committed before a previous directory sync
            # failed. An identical marker retry must still confirm durability.
            _sync_directory(directory)


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
    avoid acknowledging another window's concurrent, unseen decisions. A
    previously displayed retention notice or corrected override can be
    acknowledged after it resolves. Omitting messages acknowledges all current
    notices and startup decisions.
    No acknowledgment can persist a PCB or make recovery eligible for archival.
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
        overrides = _override_messages(generation)
        recorded_messages = (
            *overrides,
            *generation.get("known_override_messages", ()),
            *_retention_messages(generation, current=False),
        )
        displayed = tuple(
            (*overrides, *_retention_messages(generation))
            if messages is None
            else messages
        )
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
