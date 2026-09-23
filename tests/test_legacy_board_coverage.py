"""Active recovery survives until every saved board in its directory is safe."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import importlib
import os
from pathlib import Path
import struct
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock

import pytest

from .wx_harness import load_siblings


@pytest.fixture
def coverage() -> Iterator[ModuleType]:
    """Use real assignment provenance and pure coverage without loading KiCad."""
    with load_siblings(
        "_saved_board_coverage_tests", ("legacy_board_coverage",), {}
    ) as modules:
        yield modules["legacy_board_coverage"]


class Footprint:
    """Stateful saved-board values changed only by explicit fixture saves."""

    def __init__(
        self,
        *,
        component_id: str = "uuid-r1",
        reference: str = "R1",
        value: str = "10k",
        item: str = "R_0603",
        fields: Optional[dict[str, str]] = None,
        attributes: int = 0,
    ) -> None:
        self.component_id = component_id
        self.reference = reference
        self.value = value
        self.item = item
        self.fields = {} if fields is None else fields.copy()
        self.attributes = attributes


def live(coverage: ModuleType, **changes: Any) -> Any:
    """Create immutable current values independently of the saved board."""
    resolver = importlib.import_module(f"{coverage.__package__}.part_assignments")
    fields = changes.pop("fields", {"LCSC": "C123"})
    assignment, lcsc = resolver.resolve_assignment(fields, {}, "")
    values = {
        "component_id": "uuid-r1",
        "reference": "R1",
        "value": "10k",
        "footprint": "R_0603",
        "bom": True,
        "pos": True,
        "assignment": assignment,
        "lcsc": lcsc,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def row(**changes: Any) -> Any:
    """Model the planner contract without importing its implementation."""
    values = {
        "reference": "R1",
        "lcsc": "C123",
        "status": "accounted",
        "component_ids": ("uuid-r1",),
        "reason": "explicit native assignment takes precedence",
        "identity": ("R1", "10k", "R_0603", False, False),
        "disposition": "already_matches",
        "native_value": "C123",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def saved(path: Path, *parts: Footprint) -> Any:
    """Materialize source bytes and a detached loaded native representation."""
    path.write_text("(kicad_pcb saved source)", encoding="utf-8")
    return SimpleNamespace(GetFootprints=lambda: list(parts))


def appledouble(path: Path) -> None:
    """Write a v2 metadata header and Finder Info entry from RFC 1740's layout."""
    path.write_bytes(
        struct.pack(">II16sH", 0x00051607, 0x00020000, b"Mac OS X        ", 1)
        + struct.pack(">III", 9, 38, 32)
        + bytes(32)
    )


def captured(coverage: ModuleType, board: Any) -> tuple[Any, ...]:
    """Model the caller's native capture into detached Default-value records."""
    return tuple(
        live(
            coverage,
            component_id=part.component_id,
            reference=part.reference,
            value=part.value,
            footprint=part.item,
            bom=not bool(part.attributes & 8),
            pos=not bool(part.attributes & 4),
            fields=part.fields,
        )
        for part in board.GetFootprints()
    )


def collect(
    coverage: ModuleType,
    path: Path,
    boards: dict[str, Any],
    *,
    parts: Optional[tuple[Any, ...]] = None,
    rows: Optional[tuple[Any, ...]] = None,
    schematic_saved_ids: frozenset[str] = frozenset(),
) -> Any:
    """Exercise complete collection with every native read supplied by loading."""
    return coverage.collect_saved_board_coverage(
        str(path),
        (live(coverage),) if parts is None else parts,
        (row(),) if rows is None else rows,
        load_board=lambda pathname: captured(coverage, boards[pathname]),
        schematic_saved_ids=schematic_saved_ids,
    )


def test_native_import_requires_explicit_pcb_save(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A recovered live field is lost on discard; its source must stay active."""
    path = tmp_path / "board.kicad_pcb"
    disk = Footprint()
    boards = {str(path): saved(path, disk)}
    planned = row(status="planned", disposition="imported")

    before = collect(coverage, path, boards, rows=(planned,))
    assert not before.eligible
    assert any("board.kicad_pcb" in message for message in before.advisories)
    assert not before.diagnostics
    assert disk.fields == {}

    # The application/user explicitly saves; the helper only reloads evidence.
    disk.fields["LCSC"] = "C123"
    path.write_text("(kicad_pcb explicit user save)", encoding="utf-8")
    after = collect(coverage, path, boards, rows=(planned,))
    assert after.eligible and not after.diagnostics
    assert coverage.verify_saved_board_sources(after)


@pytest.mark.parametrize("fields", [{"LCSC": "C999"}, {"LCSC": ""}])
def test_native_choice_requires_that_exact_saved_value(
    coverage: ModuleType, tmp_path: Path, fields: dict[str, str]
) -> None:
    """A different valid saved field cannot prove a new live override or clear."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    chosen = fields["LCSC"]
    result = collect(
        coverage,
        path,
        boards,
        parts=(live(coverage, fields=fields),),
        rows=(row(native_value=chosen),),
    )
    assert not result.eligible
    assert any("uuid-r1" in message for message in result.advisories)


def test_successful_schematic_save_proves_current_assignment_durable(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Established schematic persistence may cover an unsaved current PCB field."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint())}
    result = collect(coverage, path, boards, schematic_saved_ids=frozenset({"uuid-r1"}))
    assert result.eligible


@pytest.mark.parametrize(
    "changes", [{"component_id": "other-uuid"}, {"reference": "R9"}]
)
def test_durable_native_assignment_is_matched_by_uuid(
    coverage: ModuleType, tmp_path: Path, changes: dict[str, str]
) -> None:
    """Reference reuse is not UUID persistence; annotation drift is harmless."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}, **changes))}
    result = collect(coverage, path, boards)
    assert result.eligible == ("component_id" not in changes)


@pytest.mark.parametrize("parts", [(), ({"value": "11k"},)])
def test_obsolete_live_row_still_recoverable_after_discard_retains_source(
    coverage: ModuleType, tmp_path: Path, parts: tuple[dict[str, str], ...]
) -> None:
    """Unsaved deletion and tuple edits do not make saved missing fields obsolete."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint())}
    result = collect(
        coverage,
        path,
        boards,
        parts=tuple(live(coverage, **changes) for changes in parts),
        rows=(row(status="obsolete", disposition="obsolete", native_value=None),),
        schematic_saved_ids=frozenset({"uuid-r1"}),
    )
    assert not result.eligible


def test_truly_obsolete_row_does_not_block_archival(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Rows absent from all saved boards survive in the archive without blocking."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path)}
    result = collect(
        coverage,
        path,
        boards,
        parts=(),
        rows=(row(status="obsolete", component_ids=(), native_value=None),),
    )
    assert result.eligible


@pytest.mark.parametrize(
    "fields", [{}, {"LCSC": "invalid"}, {"LCSC": "C123", "JLC": " "}]
)
def test_other_saved_board_recovery_blocks_current_archive(
    coverage: ModuleType, tmp_path: Path, fields: dict[str, str]
) -> None:
    """A shared-directory board can need an otherwise obsolete legacy row."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {
        str(path): saved(path),
        str(other): saved(other, Footprint(fields=fields)),
    }
    result = collect(
        coverage,
        path,
        boards,
        parts=(),
        rows=(row(status="obsolete", component_ids=(), native_value=None),),
        schematic_saved_ids=frozenset({"uuid-r1"}),
    )
    assert not result.eligible
    assert any(
        "other.kicad_pcb" in message
        for message in result.diagnostics + result.advisories
    )


@pytest.mark.parametrize("fields", [{"JLCPCB PartNr": " c999 "}, {"LCSC": ""}])
def test_saved_sibling_valid_native_choice_supersedes_legacy(
    coverage: ModuleType, tmp_path: Path, fields: dict[str, str]
) -> None:
    """An explicit saved sibling choice needs no further historical recovery."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {
        str(path): saved(path),
        str(other): saved(other, Footprint(fields=fields)),
    }
    result = collect(
        coverage,
        path,
        boards,
        parts=(),
        rows=(row(status="obsolete", component_ids=(), native_value=None),),
    )
    assert result.eligible


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "R9"},
        {"value": "11k"},
        {"item": "R_0805"},
        {"attributes": 4},
        {"attributes": 8},
    ],
)
def test_sibling_recovery_uses_complete_historical_tuple(
    coverage: ModuleType, tmp_path: Path, changes: dict[str, Any]
) -> None:
    """Similar references alone never establish a recoverable saved row."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {str(path): saved(path), str(other): saved(other, Footprint(**changes))}
    result = collect(
        coverage,
        path,
        boards,
        parts=(),
        rows=(row(status="obsolete", component_ids=(), native_value=None),),
    )
    assert result.eligible


def test_rows_with_same_reference_are_matched_independently(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A second board can own a distinct tuple under the same reference."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {
        str(path): saved(path, Footprint(fields={"LCSC": "C123"})),
        str(other): saved(other, Footprint(value="11k")),
    }
    result = collect(
        coverage,
        path,
        boards,
        rows=(
            row(),
            row(status="obsolete", identity=("R1", "11k", "R_0603", False, False)),
        ),
    )
    assert not result.eligible
    assert any("other.kicad_pcb" in message for message in result.advisories)


@pytest.mark.parametrize("which", ["current", "sibling"])
def test_read_failure_retains_active_recovery(
    coverage: ModuleType, tmp_path: Path, which: str
) -> None:
    """Read failures on either owner cannot be mistaken for an empty board."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {
        str(path): saved(path, Footprint(fields={"LCSC": "C123"})),
        str(other): saved(other),
    }
    del boards[str(path if which == "current" else other)]
    result = collect(coverage, path, boards)
    assert not result.eligible
    assert any("read" in message.lower() for message in result.diagnostics)


def test_missing_current_file_is_not_confirmed_absence(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A failed saved-board read blocks even a successful schematic export."""
    result = collect(
        coverage,
        tmp_path / "unsaved.kicad_pcb",
        {},
        schematic_saved_ids=frozenset({"uuid-r1"}),
    )
    assert not result.eligible


@pytest.mark.parametrize("change", ["content", "replace", "new", "deleted"])
def test_retirement_rechecks_board_bytes_identity_and_directory_membership(
    coverage: ModuleType, tmp_path: Path, change: str
) -> None:
    """The under-lock recheck rejects stale evidence after any relevant change."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    result = collect(coverage, path, boards)
    assert result.eligible
    if change == "content":
        stat = path.stat()
        path.write_text("(kicad_pcb other source)", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "replace":
        replacement = tmp_path / "replacement.tmp"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    elif change == "new":
        (tmp_path / "new.kicad_pcb").write_text("new board", encoding="utf-8")
    else:
        path.unlink()
    assert not coverage.verify_saved_board_sources(result)


def test_sources_changed_during_loading_are_rejected(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Snapshots must describe the same bytes that their source token covers."""
    path = tmp_path / "board.kicad_pcb"
    board = saved(path, Footprint(fields={"LCSC": "C123"}))

    def load(pathname: str) -> Any:
        Path(pathname).write_text("changed during load", encoding="utf-8")
        return captured(coverage, board)

    result = coverage.collect_saved_board_coverage(
        str(path), (live(coverage),), (row(),), load_board=load
    )
    assert not result.eligible


def test_physical_aliases_are_loaded_once_and_remain_guarded(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Symlink and hardlink aliases share one load but each path stays guarded."""
    path, alias = tmp_path / "board.kicad_pcb", tmp_path / "alias.kicad_pcb"
    board = saved(path, Footprint(fields={"LCSC": "C123"}))
    alias.symlink_to(path)
    hardlink = tmp_path / "hardlink.kicad_pcb"
    os.link(path, hardlink)
    loader = Mock(return_value=captured(coverage, board))
    result = coverage.collect_saved_board_coverage(
        str(path), (live(coverage),), (row(),), load_board=loader
    )
    assert result.eligible and len(result.snapshots) == 1
    loader.assert_called_once_with(str(path))
    assert len(result.source_tokens) == 3
    alias.unlink()
    assert not coverage.verify_saved_board_sources(result)


def test_actual_current_filename_and_only_direct_siblings_are_examined(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Alternate current extensions count; unrelated subdirectories do not."""
    path = tmp_path / "board.custom"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    subdirectory = tmp_path / "archive"
    subdirectory.mkdir()
    (subdirectory / "older.kicad_pcb").write_text("unrelated", encoding="utf-8")
    result = collect(coverage, path, boards)
    assert result.eligible
    assert result.current_path == str(path)


def test_appledouble_sidecar_is_not_loaded_as_a_saved_board(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Mac metadata beside a saved PCB cannot produce a repeated read blocker."""
    path = tmp_path / "board.kicad_pcb"
    sidecar = tmp_path / "._board.kicad_pcb"
    board = saved(path, Footprint(fields={"LCSC": "C123"}))
    appledouble(sidecar)

    def load_real_pcb(pathname: str) -> tuple[Any, ...]:
        if pathname != str(path):
            raise ValueError("AppleDouble metadata is not a PCB")
        return captured(coverage, board)

    loader = Mock(side_effect=load_real_pcb)

    result = coverage.collect_saved_board_coverage(
        str(path), (live(coverage),), (row(),), load_board=loader
    )

    assert result.eligible
    assert result.candidate_paths == (str(path),)
    assert tuple(token.path for token in result.source_tokens) == (str(path),)
    loader.assert_called_once_with(str(path))
    assert coverage.verify_saved_board_sources(result)


@pytest.mark.parametrize(
    "metadata",
    [
        b"\x00\x05\x16\x07",
        struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 1),
        struct.pack(">II16sH", 0x00051607, 0x00030000, bytes(16), 0),
        struct.pack(">II16sH", 0x00051600, 0x00020000, bytes(16), 0),
    ],
)
def test_ambiguous_metadata_filename_still_blocks_on_read_failure(
    coverage: ModuleType, tmp_path: Path, metadata: bytes
) -> None:
    """A name alone or incomplete/unsupported signature cannot waive protection."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "._unknown.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    other.write_bytes(metadata)

    result = collect(coverage, path, boards)

    assert not result.eligible
    assert str(other) in result.candidate_paths
    assert any(str(other) in message for message in result.diagnostics)


def test_actual_current_appledouble_filename_is_never_excluded(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Even a current path containing metadata requires its own saved-board read."""
    path = tmp_path / "._board.kicad_pcb"
    appledouble(path)
    loader = Mock(side_effect=ValueError("not a PCB"))

    result = coverage.collect_saved_board_coverage(
        str(path), (live(coverage),), (row(),), load_board=loader
    )

    assert not result.eligible
    assert result.candidate_paths == (str(path),)
    loader.assert_called_once_with(str(path))


def test_appledouble_signature_without_sidecar_name_remains_a_candidate(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """An ordinary sibling's content cannot silently bypass its saved-board read."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    appledouble(other)
    result = collect(coverage, path, boards)
    assert not result.eligible
    assert str(other) in result.candidate_paths


def test_sidecar_replaced_during_header_read_is_not_excluded(
    coverage: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata predicate verifies the name still owns the bytes it inspected."""
    path, sidecar = tmp_path / "board.kicad_pcb", tmp_path / "._board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    appledouble(sidecar)
    original = open
    replaced = False

    @contextmanager
    def replace_after_read(pathname: str, *args: Any, **kwargs: Any) -> Iterator[Any]:
        nonlocal replaced
        with original(pathname, *args, **kwargs) as source:
            if pathname != str(sidecar) or replaced:
                yield source
                return

            def read(size: int) -> bytes:
                nonlocal replaced
                data = source.read(size)
                replacement = tmp_path / "replacement.tmp"
                boards[str(sidecar)] = saved(replacement, Footprint())
                replacement.replace(sidecar)
                replaced = True
                return data

            yield SimpleNamespace(fileno=source.fileno, read=read)

    monkeypatch.setattr(coverage, "open", replace_after_read, raising=False)
    result = collect(coverage, path, boards)

    assert not result.eligible
    assert str(sidecar) in result.candidate_paths


@pytest.mark.parametrize("change", ["added", "deleted", "replaced"])
def test_metadata_only_changes_do_not_invalidate_archival_sources(
    coverage: ModuleType, tmp_path: Path, change: str
) -> None:
    """The under-lock directory scan applies the same metadata exclusion."""
    path, sidecar = tmp_path / "board.kicad_pcb", tmp_path / "._board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    if change != "added":
        appledouble(sidecar)
    result = collect(coverage, path, boards)
    assert result.eligible

    if change == "deleted":
        sidecar.unlink()
    elif change == "replaced":
        replacement = tmp_path / "replacement.tmp"
        appledouble(replacement)
        replacement.replace(sidecar)
    else:
        appledouble(sidecar)

    assert coverage.verify_saved_board_sources(result)


@pytest.mark.parametrize("when", ["before_recheck", "during_token_read"])
def test_sidecar_becoming_a_real_board_rejects_stale_archival(
    coverage: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """A former metadata path cannot conceal a new owner during either recheck."""
    path, sidecar = tmp_path / "board.kicad_pcb", tmp_path / "._board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    appledouble(sidecar)
    result = collect(coverage, path, boards)
    assert result.eligible
    if when == "before_recheck":
        saved(sidecar, Footprint())
    else:
        original = coverage._source_token

        def replace_sidecar_after_read(pathname: str) -> Any:
            token = original(pathname)
            saved(sidecar, Footprint())
            return token

        monkeypatch.setattr(coverage, "_source_token", replace_sidecar_after_read)

    assert not coverage.verify_saved_board_sources(result)


def test_unreadable_metadata_named_file_remains_a_disclosed_blocker(
    coverage: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to identify a metadata-looking file is never evidence of no owner."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "._board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    appledouble(other)
    original = open

    def unreadable_sidecar(pathname: str, *args: Any, **kwargs: Any) -> Any:
        if pathname == str(other):
            raise PermissionError("permission denied")
        return original(pathname, *args, **kwargs)

    monkeypatch.setattr(coverage, "open", unreadable_sidecar, raising=False)
    result = collect(coverage, path, boards)

    assert not result.eligible
    assert str(other) in result.candidate_paths


@pytest.mark.parametrize("component_id", ["", "uuid-r1"])
def test_corrupt_saved_uuid_schema_blocks_retirement(
    coverage: ModuleType, tmp_path: Path, component_id: str
) -> None:
    """Malformed footprint identity prevents a trustworthy saved-board read."""
    path = tmp_path / "board.kicad_pcb"
    boards = {
        str(path): saved(
            path,
            Footprint(fields={"LCSC": "C123"}),
            Footprint(component_id=component_id),
        )
    }
    result = collect(coverage, path, boards)
    assert not result.eligible


def test_planned_assignment_must_have_been_applied_before_archival(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A planned value and matching saved file cannot replace failed native apply."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    result = collect(
        coverage,
        path,
        boards,
        parts=(live(coverage, fields={}),),
        rows=(row(status="planned"),),
        schematic_saved_ids=frozenset({"uuid-r1"}),
    )
    assert not result.eligible


def test_unresolved_plan_and_incomplete_row_identity_fail_closed(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """The archive guard independently refuses incomplete planner evidence."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    assert not collect(
        coverage, path, boards, rows=(row(status="unresolved"),)
    ).eligible
    assert not collect(coverage, path, boards, rows=(row(identity=()),)).eligible


def test_blocked_coverage_cannot_be_used_as_retirement_callback(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A source recheck cannot promote previously rejected recovery coverage."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint())}
    result = collect(coverage, path, boards)
    assert not coverage.verify_saved_board_sources(result)
    # Failed coverage cannot be turned into successful evidence by losing errors.
    assert not coverage.verify_saved_board_sources(replace(result, source_tokens=()))


def test_inode_less_filesystem_does_not_merge_unrelated_boards(
    coverage: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unavailable physical IDs cannot make one safe board cover every sibling."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "other.kicad_pcb"
    boards = {
        str(path): saved(path, Footprint(fields={"LCSC": "C123"})),
        str(other): saved(other, Footprint()),
    }
    original = coverage._source_token
    monkeypatch.setattr(
        coverage, "_source_token", lambda pathname: replace(original(pathname), inode=0)
    )
    result = collect(coverage, path, boards)
    assert not result.eligible
    assert len(result.snapshots) == 2


def test_mixed_case_sibling_extension_still_protects_recovery(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A PCB filename's case cannot hide another recovery owner."""
    path, other = tmp_path / "board.kicad_pcb", tmp_path / "Other.KICAD_PCB"
    boards = {
        str(path): saved(path, Footprint(fields={"LCSC": "C123"})),
        str(other): saved(other, Footprint()),
    }
    result = collect(coverage, path, boards)
    assert not result.eligible
    assert any("Other.KICAD_PCB" in message for message in result.advisories)


def test_sibling_added_during_final_token_read_rejects_archival(
    coverage: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retirement callback rechecks membership after lengthy content reads."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    result = collect(coverage, path, boards)
    assert result.eligible
    original = coverage._source_token

    def create_sibling_after_read(pathname: str) -> Any:
        token = original(pathname)
        (tmp_path / "new.kicad_pcb").write_text("new owner", encoding="utf-8")
        return token

    monkeypatch.setattr(coverage, "_source_token", create_sibling_after_read)
    assert not coverage.verify_saved_board_sources(result)


def test_symlink_target_replacement_rejects_archival(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """A surviving alias pathname must still refer to the inspected physical file."""
    path, alias = tmp_path / "board.kicad_pcb", tmp_path / "alias.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    alias.symlink_to(path)
    result = collect(coverage, path, boards)
    assert result.eligible
    target = tmp_path / "another.source"
    target.write_bytes(path.read_bytes())
    alias.unlink()
    alias.symlink_to(target)
    assert not coverage.verify_saved_board_sources(result)


def test_current_alias_keeps_successful_schematic_durability_exemption(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """One physical current PCB is not a second owner merely through its alias."""
    path, alias = tmp_path / "board.kicad_pcb", tmp_path / "alias.kicad_pcb"
    boards = {str(path): saved(path, Footprint())}
    alias.symlink_to(path)
    result = collect(coverage, path, boards, schematic_saved_ids=frozenset({"uuid-r1"}))
    assert result.eligible
    assert len(result.snapshots) == 1


@pytest.mark.parametrize("empty", [True, False])
@pytest.mark.parametrize(
    "source_problem", ["missing_current", "unreadable_sibling", "no_filename"]
)
def test_no_recoverable_rows_need_no_saved_board_evidence(
    coverage: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty: bool,
    source_problem: str,
) -> None:
    """Empty or wholly invalid legacy data cannot require unrelated PCB reads."""
    path = tmp_path / "board.kicad_pcb"
    current_path = str(path)
    if source_problem == "unreadable_sibling":
        saved(path)
        (tmp_path / "unreadable.kicad_pcb").write_text("malformed", encoding="utf-8")
    elif source_problem == "no_filename":
        current_path = ""
    rows = () if empty else (row(status="ignored", lcsc="invalid", component_ids=()),)
    loader = Mock(side_effect=ValueError("malformed unrelated PCB"))
    scan = Mock(
        side_effect=AssertionError("No recoverable rows need no directory scan")
    )
    monkeypatch.setattr(coverage, "_candidate_paths", scan)

    result = coverage.collect_saved_board_coverage(
        current_path, (), rows, load_board=loader
    )

    assert result.eligible
    assert not result.requires_sources
    assert not result.diagnostics and not result.advisories
    assert (
        not result.source_tokens and not result.snapshots and not result.candidate_paths
    )
    assert coverage.verify_saved_board_sources(result)
    loader.assert_not_called()
    scan.assert_not_called()


def test_obsolete_valid_row_still_requires_saved_board_evidence(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Current obsolescence says nothing about another saved board's recovery."""
    result = collect(
        coverage,
        tmp_path / "missing.kicad_pcb",
        {},
        parts=(),
        rows=(row(status="obsolete", component_ids=(), native_value=None),),
    )
    assert not result.eligible
    assert result.requires_sources
    assert not coverage.verify_saved_board_sources(result)


def test_missing_tokens_cannot_imply_no_source_requirement(
    coverage: ModuleType, tmp_path: Path
) -> None:
    """Only the explicit no-recoverable-rows outcome authorizes empty evidence."""
    path = tmp_path / "board.kicad_pcb"
    boards = {str(path): saved(path, Footprint(fields={"LCSC": "C123"}))}
    result = collect(coverage, path, boards)
    assert result.eligible and result.requires_sources
    assert not coverage.verify_saved_board_sources(replace(result, source_tokens=()))
    assert not coverage.verify_saved_board_sources(
        replace(result, requires_sources=False)
    )
