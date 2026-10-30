"""Use the production verified parser and native owner for variant review copies."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest

from .fabrication_test_support import Point, modules

__all__ = ["modules"]


def _exporter(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, SimpleNamespace]:
    state = SimpleNamespace(clones=[], destroyed=[], paths=[], failure="")
    project = SimpleNamespace(
        variables={"REVISION": "unsaved revision"}, settings="source"
    )

    class Board:
        def __init__(self) -> None:
            self.thisown = False
            self.this = len(state.clones) + 1
            self.variant = "B"
            self.filename = str(tmp_path / "board.kicad_pcb")
            self.project = project
            self.properties = {"REVISION": "saved revision"}

        @staticmethod
        def __swig_destroy__(pointer: int) -> None:
            assert pointer not in state.destroyed
            state.destroyed.append(pointer)

        def GetFileName(self) -> str:
            return self.filename

        def SetFileName(self, value: str) -> None:
            self.filename = value

        def Footprints(self) -> tuple[Any, ...]:
            return ()

        def GetDesignSettings(self) -> Any:
            return SimpleNamespace(GetAuxOrigin=lambda: Point(0, 0))

        def GetProject(self) -> Any:
            return self.project

        def SetProject(self, value: Any, reference_only: bool) -> None:
            assert reference_only is True
            if state.failure == "project":
                raise RuntimeError("project unavailable")
            self.project = value

        def SetCurrentVariant(self, name: str) -> None:
            self.variant = "" if state.failure == "variant" else name

        def GetCurrentVariant(self) -> str:
            return self.variant

        def SynchronizeProperties(self) -> None:
            if state.failure == "properties":
                raise RuntimeError("properties unavailable")
            self.properties = dict(self.project.variables)

        def GetProperties(self) -> dict[str, str]:
            return self.properties

    source = Board()
    source.this = 0
    Path(source.filename).write_bytes(b"original PCB bytes")
    parent = SimpleNamespace(settings={})
    exporter = modules.fabrication.Fabrication(parent, source)
    monkeypatch.setattr(
        exporter,
        "_board_content",
        lambda: json.dumps(source.properties).encode("utf-8"),
    )

    class IO:
        def DoLoad(self, content: Any, *_args: Any) -> Any:
            document, path = content
            state.paths.append(Path(path))
            assert Path(path).read_text(encoding="utf-8") == document
            clone = Board()
            clone.properties = json.loads(document)
            clone.project = None
            clone.filename = path
            state.clones.append(clone)
            return clone

    native = SimpleNamespace(
        PCB_IO_KICAD_SEXPR=IO,
        STRING_LINE_READER=lambda content, path: (content, path),
    )
    monkeypatch.setattr(modules.fabrication, "import_module", lambda _name: native)
    state.source, state.project = source, project
    return exporter, state


@pytest.mark.parametrize("name", ["", "A"])
def test_variant_review_copy_preserves_source_and_releases_exactly_once(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    exporter, state = _exporter(modules, tmp_path, monkeypatch)
    source_before = deepcopy(state.source.__dict__)
    project_before = deepcopy(state.project.__dict__)
    owner = exporter.copy_variant_board(name)
    clone = owner.board
    assert clone.GetCurrentVariant() == name
    assert clone.GetFileName() == state.source.GetFileName()
    assert clone.GetProject() is state.project
    assert clone.GetProperties() == state.project.variables
    assert not clone.thisown
    assert not exporter._generation
    assert not exporter._ordinary_generation
    assert state.source.__dict__ == source_before
    assert state.project.__dict__ == project_before
    assert Path(state.source.filename).read_bytes() == b"original PCB bytes"
    assert all(not path.exists() for path in state.paths)
    owner.close()
    owner.close()
    assert state.destroyed == [clone.this]
    assert not clone._jlcpcb_release.alive
    with pytest.raises(RuntimeError, match="closed"):
        _ = owner.board


@pytest.mark.parametrize("failure", ["variant", "project", "properties"])
def test_failed_variant_review_preparation_releases_the_detached_allocation(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    exporter, state = _exporter(modules, tmp_path, monkeypatch)
    state.failure = failure
    with pytest.raises(RuntimeError):
        exporter.copy_variant_board("A")
    assert state.destroyed == [state.clones[0].this]
    assert not state.clones[0]._jlcpcb_release.alive
    assert state.source.variant == "B"
    assert state.project.settings == "source"
    assert all(not path.exists() for path in state.paths)


@pytest.mark.parametrize("failure", ["validation", "properties"])
def test_failed_plot_clone_adoption_releases_the_detached_allocation(
    modules: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """An unadopted clone must not remain owned by a retained failure traceback."""
    exporter, state = _exporter(modules, tmp_path, monkeypatch)
    if failure == "properties":
        state.failure = failure
    validations = [0]

    def validate() -> None:
        validations[0] += 1
        if failure == "validation" and validations[0] == 2:
            raise RuntimeError("source changed during parsing")

    monkeypatch.setattr(exporter, "validate_generation", validate)
    with TemporaryDirectory(dir=tmp_path) as directory:
        operation = SimpleNamespace(
            directory=SimpleNamespace(name=directory),
            output=modules.fabrication.OutputAssemblySnapshot((), ()),
            plot_board=None,
            plot_source_digest=None,
            plot_project_properties=(),
        )
        exporter._generation = operation
        exporter.variant_name = "A"
        with pytest.raises(RuntimeError) as caught:
            exporter._get_plot_board()
        assert caught.value.__traceback__ is not None
        assert state.destroyed == [state.clones[0].this]
        assert not state.clones[0]._jlcpcb_release.alive
        assert operation.plot_board is None
        assert operation.plot_source_digest is None
        assert operation.plot_project_properties == ()
        assert state.source.variant == "B"
        assert state.project.settings == "source"
        exporter._generation = None
