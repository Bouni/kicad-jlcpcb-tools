"""Invalidate impedance review for relevant adjacent native project definitions."""

import json
from pathlib import Path
from typing import Any

import pytest

from impedance.project_context import ProjectContextError, project_context_records


def _project() -> dict[str, Any]:
    """Describe native settings with both relevant values and unrelated UI metadata."""
    return {
        "meta": {"version": 1, "filename": "example.kicad_pro"},
        "board": {
            "design_settings": {
                "rules": {"min_clearance": 0.2},
                "meta": {"version": 2},
            },
            "viewports": [{"x": 10}],
        },
        "net_settings": {
            "meta": {"version": 4},
            "classes": [{"name": "Default", "clearance": 0.2}],
        },
        "text_variables": {"RF_GAP": "0.2mm", "meta": "real user variable"},
        "schematic": {"drawing": "unrelated"},
    }


def _write_project(path: Path, document: Any) -> None:
    """Write a native project fixture inside pytest's temporary directory."""
    path.write_text(json.dumps(document), encoding="utf-8")


def test_absence_is_explicit_deterministic_and_does_not_require_board_read(
    tmp_path: Path,
) -> None:
    """A saved filename can be fingerprinted without opening or rewriting its PCB."""
    board = tmp_path / "board.kicad_pcb"
    records = project_context_records(board)
    assert records == project_context_records(board)
    assert "project-context:kicad_pro:absent" in records
    assert "project-context:kicad_dru:absent" in records
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("suffix", [".kicad_pro", ".kicad_dru"])
def test_creation_change_and_deletion_invalidate_review(
    tmp_path: Path, suffix: str
) -> None:
    """Adding, editing, or removing an external definition must alter review context."""
    board = tmp_path / "board.kicad_pcb"
    definition = tmp_path / ("shared" + suffix)
    absent = project_context_records(board)
    if suffix == ".kicad_pro":
        _write_project(definition, _project())
    else:
        definition.write_text('(version 1)\n(rule "RF")\n', encoding="utf-8")
    created = project_context_records(board)
    if suffix == ".kicad_pro":
        document = _project()
        document["net_settings"]["classes"][0]["clearance"] = 0.4
        _write_project(definition, document)
    else:
        definition.write_text('(version 1)\n(rule "Changed")\n', encoding="utf-8")
    changed = project_context_records(board)
    definition.unlink()
    assert absent != created != changed
    assert project_context_records(board) == absent


@pytest.mark.parametrize("selected", ["design", "net", "text", "text_meta"])
def test_each_selected_native_setting_changes_context(
    tmp_path: Path, selected: str
) -> None:
    """Never discard netclass, design-rule, or user text-variable edits as metadata."""
    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    document = _project()
    _write_project(path, document)
    before = project_context_records(board)
    if selected == "design":
        document["board"]["design_settings"]["rules"]["min_clearance"] = 0.5
    elif selected == "net":
        document["net_settings"]["classes"].append({"name": "RF", "clearance": 0.6})
    elif selected == "text":
        document["text_variables"]["RF_GAP"] = "0.5mm"
    else:
        document["text_variables"]["meta"] = "changed user variable"
    _write_project(path, document)
    assert project_context_records(board) != before


def test_json_order_whitespace_metadata_and_view_changes_are_ignored(
    tmp_path: Path,
) -> None:
    """Review must survive formatting, version metadata, and unrelated view state."""
    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    document = _project()
    _write_project(path, document)
    before = project_context_records(board)
    document["meta"] = {"filename": "new", "version": 99}
    document["board"]["viewports"] = [{"x": 1000}]
    document["board"]["design_settings"]["meta"] = {"version": 99}
    document["net_settings"]["meta"] = {"version": 99}
    document["schematic"] = {"drawing": "different"}
    path.write_text(
        "\ufeff" + json.dumps(document, sort_keys=True, indent=3), encoding="utf-8"
    )
    assert project_context_records(board) == before


def test_all_adjacent_projects_are_included_and_names_are_recorded(
    tmp_path: Path,
) -> None:
    """A differently named shared project or rules file still affects this PCB."""
    board = tmp_path / "board.kicad_pcb"
    for filename in ("z-other.kicad_pro", "a-shared.kicad_pro", "shared.kicad_dru"):
        path = tmp_path / filename
        path.write_text("{}" if path.suffix == ".kicad_pro" else "(version 1)")
    before = project_context_records(board)
    assert all(
        any(filename in record for record in before)
        for filename in ("z-other.kicad_pro", "a-shared.kicad_pro", "shared.kicad_dru")
    )
    (tmp_path / "z-other.kicad_pro").rename(tmp_path / "renamed.kicad_pro")
    assert project_context_records(board) != before


def test_directory_enumeration_order_does_not_change_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem enumeration order is not part of the review approval contract."""
    for filename in ("z.kicad_pro", "a.kicad_pro", "r.kicad_dru"):
        path = tmp_path / filename
        path.write_text("{}" if path.suffix == ".kicad_pro" else "(version 1)")
    board = tmp_path / "board.kicad_pcb"
    before = project_context_records(board)
    original = Path.iterdir

    def reverse_entries(self: Path) -> Any:
        """Enumerate the same adjacent filenames in the opposite order."""
        return iter(reversed(tuple(original(self))))

    monkeypatch.setattr(Path, "iterdir", reverse_entries)
    assert project_context_records(board) == before


def test_regular_file_symlink_reads_content_under_its_adjacent_name(
    tmp_path: Path,
) -> None:
    """Explicit adjacent links are definitions; unrelated paths are never searched."""
    directory = tmp_path / "pcb"
    directory.mkdir()
    target = tmp_path / "shared-data.json"
    _write_project(target, _project())
    adjacent = directory / "shared.kicad_pro"
    adjacent.symlink_to(target)
    board = directory / "board.kicad_pcb"
    before = project_context_records(board)
    assert any("shared.kicad_pro" in record for record in before)
    _write_project(target, {"net_settings": {"classes": []}})
    assert project_context_records(board) != before


def test_only_exact_adjacent_native_definition_files_are_read(tmp_path: Path) -> None:
    """Do not walk parents/children or inspect unrelated backups and local settings."""
    directory = tmp_path / "pcb"
    directory.mkdir()
    board = directory / "board.kicad_pcb"
    before = project_context_records(board)
    for path in (
        board,
        directory / "notes.txt",
        directory / "board.kicad_prl",
        directory / "board.kicad_pro.bak",
        tmp_path / "parent.kicad_pro",
    ):
        path.write_bytes(b"not valid JSON")
    nested = directory / "child"
    nested.mkdir()
    (nested / "child.kicad_pro").write_bytes(b"invalid nested JSON")
    assert project_context_records(board) == before


def test_read_only_fingerprints_detect_same_mtime_content_changes(
    tmp_path: Path,
) -> None:
    """Hash actual bytes every time without creating files or relying on timestamps."""
    import os

    board, rules = tmp_path / "board.kicad_pcb", tmp_path / "shared.kicad_dru"
    rules.write_bytes(b"first rules")
    initial_stat = rules.stat()
    initial_names = sorted(path.name for path in tmp_path.iterdir())
    before = project_context_records(board)
    assert rules.read_bytes() == b"first rules"
    assert rules.stat().st_mtime_ns == initial_stat.st_mtime_ns
    assert sorted(path.name for path in tmp_path.iterdir()) == initial_names
    rules.write_bytes(b"other rules")
    os.utime(rules, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    assert project_context_records(board) != before


@pytest.mark.parametrize(
    "source",
    [
        "{broken",
        "[]",
        "null",
        '{"net_settings":{},"net_settings":{}}',
        '{"board":{"design_settings":{"value":NaN}}}',
        '{"net_settings":{"value":Infinity}}',
        '{"net_settings":{"value":-Infinity}}',
    ],
)
def test_invalid_or_ambiguous_json_fails_closed(tmp_path: Path, source: str) -> None:
    """Never silently substitute missing state for unreadable/ambiguous JSON."""
    (tmp_path / "settings.kicad_pro").write_text(source, encoding="utf-8")
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")


def test_non_utf8_native_project_fails_closed(tmp_path: Path) -> None:
    """Do not discard unreadable project definitions or replace malformed bytes."""
    (tmp_path / "settings.kicad_pro").write_bytes(b"\xff\xfeinvalid")
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")


@pytest.mark.parametrize("value", [None, [], "bad", 3, True])
@pytest.mark.parametrize("selected", ["board", "design", "net", "text"])
def test_present_malformed_selected_containers_are_not_treated_as_absent(
    tmp_path: Path, selected: str, value: Any
) -> None:
    """Reject changed selected schemas while accepting genuinely absent settings."""
    document = (
        {"board": {"design_settings": value} if selected == "design" else value}
        if selected in {"board", "design"}
        else {"net_settings" if selected == "net" else "text_variables": value}
    )
    _write_project(tmp_path / "settings.kicad_pro", document)
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")


def test_unknown_selected_fields_are_preserved_not_silently_skipped(
    tmp_path: Path,
) -> None:
    """Future rule fields still invalidate review without requiring schema pinning."""
    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    _write_project(path, {"net_settings": {"future_rule": {"clearance": 1}}})
    before = project_context_records(board)
    _write_project(path, {"net_settings": {"future_rule": {"clearance": 2}}})
    assert project_context_records(board) != before


@pytest.mark.parametrize("selected", ["design", "net", "text"])
def test_absent_and_present_empty_settings_are_distinct(
    tmp_path: Path, selected: str
) -> None:
    """Explicit presence remains part of the native definition's review identity."""
    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    _write_project(path, {})
    before = project_context_records(board)
    document = (
        {"board": {"design_settings": {}}}
        if selected == "design"
        else {"net_settings" if selected == "net" else "text_variables": {}}
    )
    _write_project(path, document)
    assert project_context_records(board) != before


def test_nested_rule_fields_named_meta_remain_meaningful(tmp_path: Path) -> None:
    """Exclude native container metadata, not arbitrary future nested rule fields."""
    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    _write_project(path, {"net_settings": {"future_rule": {"meta": "first"}}})
    before = project_context_records(board)
    _write_project(path, {"net_settings": {"future_rule": {"meta": "other"}}})
    assert project_context_records(board) != before


def test_overflowing_selected_numbers_fail_closed(tmp_path: Path) -> None:
    """Reject an otherwise valid JSON number that cannot be fingerprinted finitely."""
    (tmp_path / "settings.kicad_pro").write_text(
        '{"net_settings":{"future_value":1e10000}}', encoding="utf-8"
    )
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")


def test_same_mtime_equal_size_project_edits_invalidate(tmp_path: Path) -> None:
    """Canonical project settings are freshly read, not cached by size or mtime."""
    import os

    board, path = tmp_path / "board.kicad_pcb", tmp_path / "settings.kicad_pro"
    document = _project()
    _write_project(path, document)
    original = path.stat()
    before = project_context_records(board)
    document["board"]["design_settings"]["rules"]["min_clearance"] = 0.4
    _write_project(path, document)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    assert project_context_records(board) != before


@pytest.mark.parametrize("value", [None, [], {}, 1, True])
def test_text_variable_values_must_be_native_strings(
    tmp_path: Path, value: Any
) -> None:
    """Do not accept an unfamiliar text-variable schema as a valid reference."""
    _write_project(tmp_path / "settings.kicad_pro", {"text_variables": {"RF": value}})
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")


@pytest.mark.parametrize("suffix", [".kicad_pro", ".kicad_dru"])
@pytest.mark.parametrize("kind", ["directory", "dangling_symlink"])
def test_non_file_definition_entries_fail_closed(
    tmp_path: Path, suffix: str, kind: str
) -> None:
    """An unreadable definition entry must not disappear from the snapshot."""
    path = tmp_path / ("settings" + suffix)
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing")
    with pytest.raises(ProjectContextError, match=path.name):
        project_context_records(tmp_path / "board.kicad_pcb")


def test_read_and_listing_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission failures and racing deletion cannot produce an approved empty set."""
    path = tmp_path / "settings.kicad_pro"
    _write_project(path, {})

    def denied_read(self: Path) -> bytes:
        """Model a definition disappearing or becoming unreadable during capture."""
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "read_bytes", denied_read)
    with pytest.raises(ProjectContextError, match="settings.kicad_pro"):
        project_context_records(tmp_path / "board.kicad_pcb")
    monkeypatch.undo()

    def denied_listing(self: Path) -> Any:
        """Model a project directory that cannot be enumerated."""
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "iterdir", denied_listing)
    with pytest.raises(ProjectContextError, match="directory"):
        project_context_records(tmp_path / "board.kicad_pcb")


@pytest.mark.parametrize("name", ["", "board", "board.kicad_pro"])
def test_requires_a_saved_native_pcb_filename(tmp_path: Path, name: str) -> None:
    """Reject an absent filename before inspecting an unrelated working directory."""
    with pytest.raises(ProjectContextError, match="saved PCB"):
        project_context_records(tmp_path / name)


def test_relative_board_filename_does_not_scan_the_working_directory() -> None:
    """The caller must supply the saved absolute path instead of implicit cwd scope."""
    with pytest.raises(ProjectContextError, match="absolute saved PCB"):
        project_context_records(Path("board.kicad_pcb"))
