"""Protect the editor while snapshotting unsaved, already-filled PCB geometry."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.board_copy import BoardCopyError, copy_for_render, empty_board


class Board:
    """Keep writable board state independent of the source's native objects."""

    def __init__(self) -> None:
        self.thisown = False
        self.settings = {"thickness": 1.8}
        self.page = "A3"
        self.title = "Unsaved PCB"
        self.filename = "/project/unsaved-edits.kicad_pcb"
        self.properties = {"revision": "preview"}
        self.layers = (0, 4, 2)
        self.items: dict[str, list[Any]] = {
            "GetFootprints": [],
            "GetTracks": [],
            "GetDrawings": [],
            "Zones": [],
        }
        self.layer_names = {0: "F.Cu", 4: "In1.Cu", 2: "B.Cu"}
        self.layer_types = {0: "signal", 4: "power", 2: "signal"}

    def __getattr__(self, name: str) -> Any:
        """Implement value-copy native settings setters and read-only collections."""
        if name in self.items:
            return lambda: tuple(self.items[name])
        fields = {
            "DesignSettings": "settings",
            "PageSettings": "page",
            "TitleBlock": "title",
            "Properties": "properties",
            "FileName": "filename",
        }
        field = fields.get(name[3:])
        if field and name.startswith("Get"):
            return lambda: getattr(self, field)
        if field and name.startswith("Set"):
            return lambda value: setattr(self, field, deepcopy(value))
        raise AttributeError(name)

    def GetEnabledLayers(self) -> Any:
        """Expose the enabled-layer sequence, including custom copper layers."""
        return SimpleNamespace(Seq=lambda: self.layers)

    def SetEnabledLayers(self, value: Any) -> None:
        """Copy layer membership instead of recording a setter-only call."""
        self.layers = tuple(value.Seq())

    def GetLayerName(self, layer: int) -> str:
        """Read the current label."""
        return self.layer_names[layer]

    def SetLayerName(self, layer: int, name: str) -> None:
        """Preserve renamed layers on the settings shadow."""
        self.layer_names[layer] = name

    def GetLayerType(self, layer: int) -> str:
        """Read the copper layer's signal/power classification."""
        return self.layer_types[layer]

    def SetLayerType(self, layer: int, value: str) -> None:
        """Copy a layer's signal/power classification."""
        self.layer_types[layer] = value


class Output:
    """Model the native string formatter."""

    text = ""

    def GetString(self) -> str:
        """Read the board document emitted by full settings-only formatting."""
        return self.text


def module(source: Board, **changes: Any) -> Any:
    """Create native doubles that trap source mutation and observe temp lifetimes."""
    state = SimpleNamespace(paths=[], formatted=[], snapshots=[], loads=0, destroyed=[])

    class IO:
        """Model raw parsing and const item formatting, never global loading."""

        def __init__(self) -> None:
            self.text = ""

        def LoadBoard(self, filename: str, append: Any) -> Any:
            """Read while temp files exist and reconstruct independently owned data."""
            assert append is None
            path = Path(filename)
            state.paths.append(path)
            state.loads += 1
            document = path.read_text(encoding="utf-8")
            if changes.get("load_error") == state.loads:
                raise RuntimeError("native parser failed")
            if changes.get("null_load") == state.loads:
                return None
            result = Board()
            result.document = document
            state.snapshots.append(result)
            if state.loads == 2:
                result.items = deepcopy(source.items)
                result.settings = deepcopy(source.settings)
                for item in result.items["GetDrawings"]:
                    if isinstance(item, NativeText):
                        # A raw native load has no live PROJECT to resolve text.
                        item.shown = item.raw
                if changes.get("drop_tracks"):
                    result.items["GetTracks"] = []
            return result

        def FormatBoardToFormatter(self, output: Output, board: Board) -> None:
            """Simulate full formatting's mutation only on the detached shadow."""
            assert board is not source
            board.font_cache_changed = True
            state.formatted.append(board)
            output.text = changes.get(
                "header", '(kicad_pcb (version 20260206) (paper "A3"))'
            )

        def Format(self, item: Any) -> None:
            """Serialize the source item without modifying it or its parent."""
            assert item is not source
            self.text += item.serialized

        def GetStringOutput(self, clear: bool) -> str:
            """Respect the actual formatter buffer-clear contract."""
            result = self.text
            if clear:
                self.text = ""
            return result

    def forbidden(*args: Any) -> None:
        """Reject APIs that mutate a live board, project, or global editor state."""
        pytest.fail("Unsafe global board constructor/load/save/fill used")

    def destroy(board: Board) -> None:
        """Delete only a detached native allocation, at most once."""
        assert board is not source
        assert board in state.snapshots
        assert board not in state.destroyed
        state.destroyed.append(board)

    forbidden.__swig_destroy__ = destroy

    return SimpleNamespace(
        PCB_IO_KICAD_SEXPR=IO,
        STRING_FORMATTER=Output,
        BOARD=forbidden,
        LoadBoard=forbidden,
        SaveBoard=forbidden,
        ZONE_FILLER=forbidden,
        state=state,
    )


def zone(
    *,
    filled: bool = True,
    stale: bool = False,
    copper: bool = True,
    rule_area: bool = False,
) -> Any:
    """Represent a copper zone with native read-only freshness accessors."""
    return SimpleNamespace(
        serialized='(zone (net "GND") (fill yes) (filled_polygon (pts (xy 1 2))))',
        IsFilled=lambda: filled,
        NeedRefill=lambda: stale,
        IsOnCopperLayer=lambda: copper,
        GetIsRuleArea=lambda: rule_area,
    )


def test_empty_board_uses_raw_parser_and_cleans_temporary_file() -> None:
    """Work inside the editor where BOARD() intentionally returns null."""
    api = module(Board())
    with empty_board(api) as result:
        assert result is api.state.snapshots[0]
        assert result.thisown is False
        assert "(kicad_pcb" in result.document
        assert api.state.destroyed == []
    assert api.state.destroyed == [result]
    assert all(
        not path.exists() and not path.parent.exists() for path in api.state.paths
    )


def test_raw_snapshot_never_promotes_swig_ownership() -> None:
    """Borrowed-to-owned promotion corrupts SWIG 4.4.1's registry refcount."""
    api = module(Board())
    with empty_board(api):
        pass
    assert api.state.snapshots[0].thisown is False


def test_shadow_released_when_item_record_is_invalid() -> None:
    """The temporary settings board must not leak on a failed native record."""
    source = Board()
    source.items["GetTracks"] = [SimpleNamespace(serialized="(segment")]
    api = module(source)
    with pytest.raises(BoardCopyError):
        copy_for_render(source, api)
    assert api.state.destroyed == api.state.snapshots


def test_detached_released_when_geometry_validation_fails() -> None:
    """Both parser allocations must be freed when the second loses geometry."""
    source = Board()
    source.items["GetTracks"] = [SimpleNamespace(serialized='(segment (net "CLK"))')]
    api = module(source, drop_tracks=True)
    with pytest.raises(BoardCopyError, match="dropped geometry"):
        copy_for_render(source, api)
    assert len(api.state.snapshots) == 2
    assert api.state.destroyed == api.state.snapshots


def test_snapshot_preserves_unsaved_geometry_fills_and_named_sparse_nets() -> None:
    """Reparse named nets so live sparse net codes cannot get remapped incorrectly."""
    source = Board()
    source.items["GetTracks"] = [
        SimpleNamespace(serialized='(segment (net "CLK") (width 0.15))', net_code=27)
    ]
    source.items["GetFootprints"] = [
        SimpleNamespace(
            serialized='(footprint "R" (pad "1" smd rect (net "GND")))', net_code=103
        )
    ]
    source.items["GetDrawings"] = [
        SimpleNamespace(serialized='(gr_text "Unsaved (net 27) text")')
    ]
    source.items["Zones"] = [zone()]
    before = deepcopy(source.__dict__)
    api = module(source)
    with copy_for_render(source, api) as result:
        assert result is not source
        assert result.GetFileName() == source.GetFileName()
        assert '(net "CLK")' in result.document
        assert '(net "GND")' in result.document
        assert "filled_polygon" in result.document
    assert source.__dict__ == before
    shadow = api.state.formatted[0]
    assert shadow.settings == source.settings
    assert shadow.page == source.page
    assert shadow.title == source.title
    assert shadow.properties == source.properties
    assert shadow.layers == source.layers
    assert shadow.layer_types == source.layer_types
    assert all(not path.exists() for path in api.state.paths)
    assert api.state.destroyed == api.state.snapshots


@pytest.mark.parametrize("kwargs", [{"filled": False}, {"stale": True}])
def test_unfilled_or_stale_copper_fails_before_parsing(kwargs: dict[str, bool]) -> None:
    """Require the user to refill deliberately rather than mutating their board."""
    source = Board()
    source.items["Zones"] = [zone(**kwargs)]
    api = module(source)
    with pytest.raises(BoardCopyError, match="Fill copper zones in KiCad"):
        copy_for_render(source, api)
    assert api.state.loads == 0


@pytest.mark.parametrize("kwargs", [{"copper": False}, {"rule_area": True}])
def test_non_copper_and_rule_area_zones_need_no_fill(kwargs: dict[str, bool]) -> None:
    """Do not demand filling from keepouts and non-copper graphical zones."""
    source = Board()
    source.items["Zones"] = [zone(filled=False, stale=True, **kwargs)]
    with copy_for_render(source, module(source)):
        pass


def test_footprint_copper_zones_are_checked_for_freshness() -> None:
    """Check footprint-local zones as well as top-level board zones."""
    source = Board()
    source.items["GetFootprints"] = [
        SimpleNamespace(Zones=lambda: [zone(filled=False)])
    ]
    with pytest.raises(BoardCopyError, match="Fill copper zones"):
        copy_for_render(source, module(source))


@pytest.mark.parametrize(
    "changes",
    [{"load_error": 1}, {"load_error": 2}, {"null_load": 1}, {"null_load": 2}],
)
def test_parse_failures_leave_source_and_files_unchanged(
    changes: dict[str, int],
) -> None:
    """Never return an incomplete snapshot or leave its temporary document behind."""
    source = Board()
    api = module(source, **changes)
    before = deepcopy(source.__dict__)
    with pytest.raises(BoardCopyError):
        copy_for_render(source, api)
    assert source.__dict__ == before
    assert all(
        not path.exists() and not path.parent.exists() for path in api.state.paths
    )
    assert api.state.destroyed == api.state.snapshots


@pytest.mark.parametrize("record", ["", "(segment", "(segment (net 27))"])
def test_invalid_or_legacy_item_records_fail_closed(record: str) -> None:
    """Do not silently omit items or combine incompatible numeric netcode formats."""
    source = Board()
    source.items["GetTracks"] = [SimpleNamespace(serialized=record)]
    api = module(source)
    with pytest.raises(BoardCopyError):
        copy_for_render(source, api)
    assert api.state.destroyed == api.state.snapshots


@pytest.mark.parametrize(
    "header", ["", "(kicad_pcb", "(footprint x)", '(kicad_pcb) (segment (net "x"))']
)
def test_malformed_shadow_documents_fail_closed(header: str) -> None:
    """Validate root boundaries before inserting native item records."""
    source = Board()
    api = module(source, header=header)
    with pytest.raises(BoardCopyError):
        copy_for_render(source, api)
    assert api.state.destroyed == api.state.snapshots


def test_missing_raw_parser_is_actionable() -> None:
    """Reject unsupported versions without falling back to stateful helpers."""
    with pytest.raises(BoardCopyError, match="snapshot"):
        empty_board(SimpleNamespace())


def test_missing_native_destructor_fails_before_allocating() -> None:
    """Do not knowingly allocate an unowned board with no supported release API."""
    api = module(Board())
    api.BOARD = SimpleNamespace()
    with pytest.raises(BoardCopyError, match="safely release"):
        empty_board(api)
    assert api.state.loads == 0
    assert api.state.paths == []


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_settings_format_failure_releases_shadow(
    exception: type[BaseException],
) -> None:
    """Also release snapshots during cancellation, not just invalid text parsing."""
    source = Board()
    api = module(source)

    def unavailable(_io: Any, _output: Any, _board: Any) -> None:
        """Represent a native full-settings formatter failure."""
        raise exception("settings formatter unavailable")

    api.PCB_IO_KICAD_SEXPR.FormatBoardToFormatter = unavailable
    expected = BoardCopyError if exception is RuntimeError else KeyboardInterrupt
    with pytest.raises(expected, match="settings formatter unavailable"):
        copy_for_render(source, api)
    assert api.state.destroyed == api.state.snapshots
    assert all(not path.exists() for path in api.state.paths)


def test_repeated_full_snapshots_release_every_allocation() -> None:
    """Previewing multiple rows leaves no owned native snapshots behind."""
    source = Board()
    api = module(source)
    for _ in range(25):
        with copy_for_render(source, api) as detached:
            assert detached.GetFileName() == source.GetFileName()
    assert len(api.state.snapshots) == 50
    assert api.state.destroyed == api.state.snapshots
    assert all(board.thisown is False for board in api.state.snapshots)
    assert source.thisown is False


def test_native_parser_silently_dropping_geometry_is_an_error() -> None:
    """A non-null board alone does not prove native parsing retained all items."""
    source = Board()
    source.items["GetTracks"] = [
        SimpleNamespace(serialized='(segment (net "CLK") (width 0.15))')
    ]
    with pytest.raises(BoardCopyError, match="dropped geometry"):
        copy_for_render(source, module(source, drop_tracks=True))


class NativeText:
    """Expose raw versus live-project-resolved text with independently writable state."""

    def __init__(self, raw: str, shown: str) -> None:
        self.raw = raw
        self.shown = shown
        self.m_Uuid = SimpleNamespace(AsString=lambda: "context-label")
        self.position = (12_000_000, 15_000_000)
        self.style = {"size": 1_000_000, "angle": 90, "layer": "F.SilkS"}
        self.writes: list[str] = []

    @property
    def serialized(self) -> str:
        """Serialize original variable syntax, like the native const formatter."""
        return f'(gr_text {json.dumps(self.raw)} (uuid "context-label"))'

    def GetClass(self) -> str:
        """Identify a known native text class without a real pcbnew import."""
        return "PCB_TEXT"

    def GetText(self) -> str:
        """Read the stored variable-bearing text."""
        return self.raw

    def GetShownText(self, allow_extra: bool) -> str:
        """Resolve through the source board's live project, not cached properties."""
        assert allow_extra is True
        return self.shown

    def SetText(self, value: str) -> None:
        """Perform a real state change so a later read verifies materialization."""
        self.writes.append(value)
        self.raw = value
        self.shown = value


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("${PROJECTNAME}", "RF project"),
        ("${RF_VERSION}", "Unsaved project revision B"),
        ("${U3:VALUE}", "50 ohm termination"),
    ],
)
def test_detached_copy_materializes_live_context_without_changing_source(
    raw: str, shown: str
) -> None:
    """A detached preview must show live variable values while preserving PCB state."""
    source = Board()
    text = NativeText(raw, shown)
    source.items["GetDrawings"] = [text]
    before = deepcopy(text.__dict__)
    with copy_for_render(source, module(source)) as result:
        copied = result.items["GetDrawings"][0]
        assert copied.GetText() == shown
        assert copied.GetShownText(True) == shown
        assert copied.position == text.position
        assert copied.style == text.style
        assert copied.m_Uuid.AsString() == text.m_Uuid.AsString()
    assert text.__dict__ == before
    assert text.writes == []


def test_live_text_resolution_failure_prevents_returning_misleading_copy() -> None:
    """Fail closed when live project label resolution cannot be performed."""
    source = Board()
    text = NativeText("${PROJECTNAME}", "RF project")

    def unavailable(_allow_extra: bool) -> str:
        """Represent a native text resolver failure."""
        raise RuntimeError("native text resolver unavailable")

    text.GetShownText = unavailable
    source.items["GetDrawings"] = [text]
    api = module(source)
    with pytest.raises(BoardCopyError, match="native text resolver unavailable"):
        copy_for_render(source, api)
    assert text.GetText() == "${PROJECTNAME}"
    assert text.writes == []
    assert api.state.loads == 0


def test_materialization_failure_releases_detached_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed native shown-text check must close both independent snapshot boards."""
    from impedance import board_copy

    source = Board()
    api = module(source)

    def unavailable(*_args: Any) -> None:
        """Represent a native read-back mismatch after detached parsing."""
        raise RuntimeError("detached text does not match")

    monkeypatch.setattr(board_copy, "apply_text_context", unavailable)
    with pytest.raises(BoardCopyError, match="detached text does not match"):
        copy_for_render(source, api)
    assert api.state.destroyed == api.state.snapshots
    assert len(api.state.destroyed) == 2
    assert all(not path.exists() for path in api.state.paths)
