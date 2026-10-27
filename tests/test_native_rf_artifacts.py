"""Prove RF preparation reuse preserves input identity and mutable isolation."""

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from tests.native_rf_artifacts import (
    NativeRFArtifactStore,
    PreparedRFArtifacts,
    RFSourceFiles,
    prepare_native_rf_board,
)


def _native_runner(arguments: list[str]) -> None:
    """Emulate a native save and report while validating the complete command."""
    assert arguments[1:8] == [
        "pcb",
        "drc",
        "--refill-zones",
        "--save-board",
        "--format",
        "json",
        "--output",
    ]
    path = Path(arguments[-1])
    path.write_bytes(path.read_bytes() + b"\nfilled")
    Path(arguments[-2]).write_text('{"unconnected_items":[]}', encoding="utf-8")


SOURCE = RFSourceFiles("rf.kicad_pcb", b"board", b"project", b"rules")


def prepare(
    store: NativeRFArtifactStore,
    source: RFSourceFiles = SOURCE,
    *,
    executable: str = "kicad-cli",
    version: str = "10.0.5",
) -> PreparedRFArtifacts:
    """Keep identical baseline CLI identity explicit in one shared call boundary."""
    return store.prepare(source, executable=executable, version=version)


@pytest.fixture
def native_runtime(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace only CLI execution, exercising the actual session artifact store."""
    executable = tmp_path / "kicad-cli"
    executable.write_bytes(b"native runtime")
    state = SimpleNamespace(
        executable=executable, versions=["10.0.1", "10.0.2"], reads=0
    )

    def version(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        state.reads += 1
        assert arguments == [str(executable), "version", "--format", "plain"]
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 30,
        }
        return subprocess.CompletedProcess(arguments, 0, stdout=state.versions.pop(0))

    def prepare_session(
        source: RFSourceFiles = SOURCE,
        run_native: Callable[[list[str]], None] = _native_runner,
    ) -> PreparedRFArtifacts:
        return prepare_native_rf_board(
            source, str(executable), request, tmp_path_factory, run_native
        )

    monkeypatch.setattr("tests.native_rf_artifacts.subprocess.run", version)
    state.prepare = prepare_session
    return state


def test_identical_native_preparation_runs_once_and_returns_independent_copies(
    tmp_path: Path,
) -> None:
    """Two assertion groups reuse preparation, retaining separately mutable files."""
    store = NativeRFArtifactStore(tmp_path / "prepared", _native_runner)
    source = replace(SOURCE)
    first = prepare(store, source)
    second = prepare(store, replace(source))
    assert store.preparations == 1
    assert first is second
    first_path = first.filled.write_to(tmp_path / "first")
    second_path = second.filled.write_to(tmp_path / "second")
    first_path.write_bytes(b"mutated geometry")
    first_path.with_suffix(".kicad_pro").write_bytes(b"mutated project")
    first_path.with_suffix(".kicad_dru").unlink()
    assert RFSourceFiles.read(second_path) == replace(source, board=b"board\nfilled")
    report = json.loads(first.report)
    report["unconnected_items"].append("mutation")
    assert json.loads(second.report) == {"unconnected_items": []}
    assert source == SOURCE


@pytest.mark.parametrize(
    "change",
    [
        {"name": "different.kicad_pcb"},
        {"board": b"changed board"},
        {"project": b"changed project"},
        {"project": None},
        {"rules": b"changed rules"},
        {"rules": None},
    ],
)
def test_every_native_input_byte_and_optional_file_presence_requires_preparation(
    tmp_path: Path, change: dict[str, object]
) -> None:
    """Similar cases or projects with different rules cannot share a DRC result."""
    store = NativeRFArtifactStore(tmp_path / "prepared", _native_runner)
    first = prepare(store)
    second = prepare(store, replace(SOURCE, **change))
    assert store.preparations == 2
    assert second is not first


@pytest.mark.parametrize(
    ("executable", "version"),
    [("other-kicad-cli", "10.0.5"), ("kicad-cli", "10.0.6")],
)
def test_native_executable_and_version_must_match(
    tmp_path: Path, executable: str, version: str
) -> None:
    """Outputs prepared by another native runtime are independent evidence."""
    store = NativeRFArtifactStore(tmp_path / "prepared", _native_runner)
    first = prepare(store)
    second = prepare(store, executable=executable, version=version)
    assert store.preparations == 2
    assert second is not first


def test_failed_native_preparation_retries_without_publishing_partial_output(
    tmp_path: Path,
) -> None:
    """A failed CLI or incomplete output must execute again and report the failure."""
    attempts = 0

    def run(arguments: list[str]) -> None:
        nonlocal attempts
        attempts += 1
        _native_runner(arguments)
        if attempts == 1:
            raise RuntimeError("native DRC failed after saving output")
        if attempts == 2:
            Path(arguments[-2]).unlink()

    store = NativeRFArtifactStore(tmp_path / "prepared", run)
    with pytest.raises(RuntimeError, match="native DRC failed"):
        prepare(store)
    with pytest.raises(FileNotFoundError):
        prepare(store)
    result = prepare(store)
    assert prepare(store) is result
    assert attempts == 3


def test_native_preparation_never_rewrites_original_source(tmp_path: Path) -> None:
    """KiCad only receives materialized copies of exact source bytes."""
    source_path = SOURCE.write_to(tmp_path / "original")
    original = RFSourceFiles.read(source_path)
    store = NativeRFArtifactStore(tmp_path / "prepared", _native_runner)
    prepare(store, original)
    assert RFSourceFiles.read(source_path) == original


def test_materialization_rejects_nonempty_destination(tmp_path: Path) -> None:
    """An absent rules file cannot silently leave old consumer rules in place."""
    (tmp_path / "old.kicad_dru").write_bytes(b"stale rules")
    with pytest.raises(AssertionError, match="destination must be empty"):
        SOURCE.write_to(tmp_path)


def test_separate_consumers_share_session_preparation_and_replaced_cli_is_fresh(
    tmp_path: Path,
    native_runtime: Any,
) -> None:
    """Cross-module ownership shares only identical inputs under the same CLI."""
    preparations = 0

    def run(arguments: list[str]) -> None:
        nonlocal preparations
        preparations += 1
        _native_runner(arguments)

    first_source = SOURCE.write_to(tmp_path / "first-source")
    second_source = SOURCE.write_to(tmp_path / "second-source")
    first = native_runtime.prepare(RFSourceFiles.read(first_source), run)
    second = native_runtime.prepare(RFSourceFiles.read(second_source), run)
    assert first is second
    assert (native_runtime.reads, preparations) == (1, 1)
    native_runtime.executable.write_bytes(b"replaced native runtime")
    replacement = native_runtime.prepare(RFSourceFiles.read(second_source), run)
    assert replacement is not first
    assert (native_runtime.reads, preparations) == (2, 2)


def test_unidentified_native_version_never_publishes_session_store(
    native_runtime: Any,
) -> None:
    """A transient version-query failure retries identification before preparing."""
    native_runtime.versions = ["", "10.0.5\n"]
    with pytest.raises(AssertionError, match="identified KiCad version"):
        native_runtime.prepare()
    prepared = native_runtime.prepare()
    assert prepared.filled.board == b"board\nfilled"
    assert native_runtime.reads == 2
