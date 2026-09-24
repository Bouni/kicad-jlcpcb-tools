"""Require fresh approval when native settings change rendered PCB context."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from impedance.matching import analyze, validate_review
from impedance.model import (
    BoardSnapshot,
    Config,
    LayerSettings,
    Specification,
    ValidationError,
)
from impedance.pcbnew_adapter import BoardSnapshotError, snapshot_board


class ContextBoard:
    """Model value-copy native setters so serialization observes actual changes."""

    def __init__(self) -> None:
        self.settings = {"thickness": 1.6, "plot_directory": "first"}
        self.page = "A4"
        self.title = {
            "title": "RF prototype",
            "date": "2026-09-06",
            "rev": "A",
            "company": "Example",
            "comments": [f"Comment {index}" for index in range(9)],
        }
        self.properties = {"RF_LABEL": "Reference A", "VERSION": "1"}
        self.layers = (0, 4, 2, 37)
        self.layer_names = {0: "Top RF", 4: "Ground", 2: "Bottom RF", 37: "F.SilkS"}
        self.layer_types = {0: "signal", 4: "power", 2: "signal", 37: "user"}
        self.font_cache_changed = False
        self.destroyed = False
        self.zones: tuple[Any, ...] = ()
        self.footprints: tuple[Any, ...] = ()

    @property
    def thisown(self) -> bool:
        """Raw parser results are borrowed; promotion is forbidden by this double."""
        return False

    def __getattr__(self, name: str) -> Any:
        """Expose the native settings getter/setter pairs with independent state."""
        field = {
            "DesignSettings": "settings",
            "PageSettings": "page",
            "TitleBlock": "title",
            "Properties": "properties",
        }.get(name[3:])
        if field and name.startswith("Get"):
            return lambda: getattr(self, field)
        if field and name.startswith("Set"):
            return lambda value: setattr(self, field, deepcopy(value))
        raise AttributeError(name)

    def GetEnabledLayers(self) -> Any:
        """Include non-copper rendered context but retain the physical copper order."""
        return SimpleNamespace(Seq=lambda: self.layers, CuStack=lambda: (0, 4, 2))

    def SetEnabledLayers(self, value: Any) -> None:
        """Copy membership for later full-board serialization."""
        self.layers = tuple(value.Seq())

    def GetLayerName(self, layer: int) -> str:
        """Read a native layer's custom label."""
        return self.layer_names[layer]

    def SetLayerName(self, layer: int, name: str) -> None:
        """Store a custom label on the detached settings board."""
        self.layer_names[layer] = name

    def GetLayerType(self, layer: int) -> str:
        """Read a native layer's signal/power classification."""
        return self.layer_types[layer]

    def SetLayerType(self, layer: int, value: str) -> None:
        """Store classification for the subsequent native-format operation."""
        self.layer_types[layer] = value

    def GetTracks(self) -> tuple[Any, ...]:
        """Expose one eligible route unchanged by context-setting edits."""
        return (
            SimpleNamespace(
                m_Uuid=SimpleNamespace(AsString=lambda: "RF-trace"),
                GetLayer=lambda: 0,
                GetWidth=lambda: 200_000,
                GetStart=lambda: SimpleNamespace(x=0, y=0),
                GetEnd=lambda: SimpleNamespace(x=120_000_000, y=0),
                GetNetname=lambda: "RF",
            ),
        )

    def GetDrawings(self) -> tuple[Any, ...]:
        """Keep a text-variable drawing identical while its resolved value changes."""
        return (SimpleNamespace(serialized='(gr_text "${TITLE} ${RF_LABEL}")'),)

    def GetFootprints(self) -> tuple[Any, ...]:
        """Expose focused footprint context, including optional local ground zones."""
        return self.footprints

    def Zones(self) -> tuple[Any, ...]:
        """Expose live board-level zones independently from footprint-local ones."""
        return self.zones


class ContextZone:
    """Keep filled geometry and native readiness flags independently mutable."""

    def __init__(self) -> None:
        self.point = "1 2"
        self.filled = True
        self.needs_refill = False

    @property
    def serialized(self) -> str:
        """Regenerate native text from current geometry, not recorded setter calls."""
        return (
            '(zone (net "GND") (fill yes) (polygon (pts (xy 0 0))) '
            f'(filled_polygon (layer "F.Cu") (pts (xy {self.point}))))'
        )

    def IsFilled(self) -> bool:
        """Report readiness even when the serialized polygon is unchanged."""
        return self.filled

    def NeedRefill(self) -> bool:
        """Report a stale native fill independently of its serialized geometry."""
        return self.needs_refill


class ContextFootprint:
    """Serialize a contained zone while exposing its separate readiness accessors."""

    def __init__(self, zone: ContextZone) -> None:
        self.zone = zone

    @property
    def serialized(self) -> str:
        """Observe the local zone's current fill in the complete footprint record."""
        return f'(footprint "Local ground" {self.zone.serialized})'

    def Zones(self) -> tuple[ContextZone, ...]:
        """Enumerate the actual footprint-local zone, not the board's zone list."""
        return (self.zone,)


def review_snapshot(board: ContextBoard, module: Any) -> BoardSnapshot:
    """Isolate context fidelity from native net-class resolution, tested separately."""
    return replace(
        snapshot_board(board, module),
        net_classes=("RF",),
        net_class_memberships=(("RF", ("RF",)),),
        net_class_context_digest="fixture-class-context",
        net_class_error="",
    )


def review_config() -> Config:
    """Select the live route by class using the current specification contract."""
    return Config(
        enabled=True,
        specifications=(
            Specification(
                "RF",
                "RF",
                "50",
                "single_ended",
                "RF",
                (LayerSettings("F.Cu", ("In1.Cu",)),),
            ),
        ),
    )


def native_module(source: ContextBoard) -> Any:
    """Serialize observed copied settings, never record-only setter calls."""
    allocations = []

    def destroy(board: ContextBoard) -> None:
        """Delete only a newly allocated shadow, once, never the editor's board."""
        assert board is not source and board in allocations and not board.destroyed
        board.destroyed = True

    class Output:
        """Represent the string formatter's owned output buffer."""

        text = ""

        def GetString(self) -> str:
            """Return the actual document emitted by the native double."""
            return self.text

    class IO:
        """Trap live full formatting and model const per-item serialization."""

        text = ""

        def LoadBoard(self, filename: str, append: Any) -> ContextBoard:
            """Allocate a different settings board through the raw loader contract."""
            assert append is None
            assert Path(filename).read_text(encoding="utf-8").startswith("(kicad_pcb")
            board = ContextBoard()
            allocations.append(board)
            return board

        def FormatBoardToFormatter(self, output: Output, board: ContextBoard) -> None:
            """Emit each native field whose rendered value matters to approval."""
            assert board is not source
            board.font_cache_changed = True
            layers = " ".join(
                f"({layer} {json.dumps(board.layer_names[layer])} {board.layer_types[layer]})"
                for layer in board.layers
            )
            title = " ".join(
                f"({name} {json.dumps(value)})"
                for name, value in board.title.items()
                if name != "comments"
            )
            comments = " ".join(
                f"(comment {index + 1} {json.dumps(value)})"
                for index, value in enumerate(board.title["comments"])
            )
            properties = " ".join(
                f"(property {json.dumps(name)} {json.dumps(value)})"
                for name, value in sorted(board.properties.items())
            )
            output.text = (
                f"(kicad_pcb (general (thickness {board.settings['thickness']})) "
                f"(layers {layers}) (paper {json.dumps(board.page)}) "
                f"(title_block {title} {comments}) {properties} "
                f"(setup (pcbplotparams (outputdirectory {json.dumps(board.settings['plot_directory'])}))))"
            )

        def Format(self, item: Any) -> None:
            """Read the unchanged drawing record without expanding or mutating it."""
            self.text = item.serialized

        def GetStringOutput(self, clear: bool) -> str:
            """Consume the native formatter buffer."""
            assert clear
            text, self.text = self.text, ""
            return text

    return SimpleNamespace(
        PCB_IO_KICAD_SEXPR=IO,
        STRING_FORMATTER=Output,
        F_Cu=0,
        In1_Cu=4,
        B_Cu=2,
        BOARD=SimpleNamespace(__swig_destroy__=destroy),
        allocations=allocations,
    )


@pytest.mark.parametrize("fail_format", [False, True])
def test_snapshot_releases_settings_shadow_on_success_and_failure(
    fail_format: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated snapshots cannot leak shadows or damage the live board handle."""
    board = ContextBoard()
    module = native_module(board)

    def fail(*args: Any) -> None:
        """Model failure after the native shadow allocation already exists."""
        raise RuntimeError("settings serialization failed")

    if fail_format:
        monkeypatch.setattr(module.PCB_IO_KICAD_SEXPR, "FormatBoardToFormatter", fail)
    try:
        for _ in range(5):
            if fail_format:
                with pytest.raises(BoardSnapshotError):
                    snapshot_board(board, module)
            else:
                snapshot_board(board, module)
    finally:
        assert module.allocations
        assert all(allocation.destroyed for allocation in module.allocations)
        assert not board.destroyed and not board.thisown


def test_unsaved_live_project_text_change_invalidates_approval() -> None:
    """An unchanged raw ${...} drawing must follow the live project, not disk metadata."""
    board = ContextBoard()
    variables = {"REV": "prototype"}
    drawing = SimpleNamespace(
        serialized='(gr_text "${REV}")',
        m_Uuid=SimpleNamespace(AsString=lambda: "project-label"),
        GetClass=lambda: "PCB_TEXT",
        GetText=lambda: "${REV}",
        GetShownText=lambda allow_extra: variables["REV"],
    )
    board.GetDrawings = lambda: (drawing,)
    module = native_module(board)
    config = review_config()
    original = analyze(config, review_snapshot(board, module))
    approved = Config.from_dict(
        replace(
            config,
            reviewed_digest=original.digest,
            included_section_ids=tuple(
                section.section_id for section in original.sections
            ),
        ).to_dict()
    )
    variables["REV"] = "production"
    updated = analyze(approved, review_snapshot(board, module))
    assert original.sections == updated.sections
    with pytest.raises(ValidationError):
        validate_review(approved, updated)


def test_live_text_resolution_failure_blocks_snapshot() -> None:
    """Unavailable live project text must not silently disappear from review checks."""
    board = ContextBoard()

    def unavailable(allow_extra: bool) -> str:
        """Model a native text resolver failure without changing source state."""
        raise RuntimeError("project text unavailable")

    board.GetDrawings = lambda: (
        SimpleNamespace(
            serialized='(gr_text "${REV}")',
            m_Uuid=SimpleNamespace(AsString=lambda: "project-label"),
            GetClass=lambda: "PCB_TEXT",
            GetText=lambda: "${REV}",
            GetShownText=unavailable,
        ),
    )
    with pytest.raises(BoardSnapshotError, match="project text unavailable"):
        snapshot_board(board, native_module(board))


@pytest.mark.parametrize(
    "changed_field",
    [
        "property-value",
        "property-added",
        "property-removed",
        "title",
        "date",
        "rev",
        "company",
        *[f"comment-{index}" for index in range(9)],
        "copper-label",
        "silk-label",
        "copper-type",
        "page",
    ],
)
def test_rendered_setting_change_invalidates_reopened_approval(
    changed_field: str,
) -> None:
    """Persisted approval must not survive a change to native label/context inputs."""
    board = ContextBoard()
    module = native_module(board)
    config = review_config()
    initial = analyze(config, review_snapshot(board, module))
    reviewed = replace(
        config,
        reviewed_digest=initial.digest,
        included_section_ids=tuple(section.section_id for section in initial.sections),
    )
    reopened = Config.from_dict(reviewed.to_dict())
    validate_review(reopened, initial)
    if changed_field == "property-value":
        board.properties["RF_LABEL"] = "Reference B"
    elif changed_field == "property-added":
        board.properties["NEW_LABEL"] = "Additional context"
    elif changed_field == "property-removed":
        del board.properties["RF_LABEL"]
    elif changed_field.startswith("comment-"):
        board.title["comments"][int(changed_field.split("-")[1])] = "Revised comment"
    elif changed_field in {"title", "date", "rev", "company"}:
        board.title[changed_field] = 'Revised "(fill yes)" text'
    elif changed_field == "copper-label":
        board.layer_names[0] = "Alternate RF"
    elif changed_field == "silk-label":
        board.layer_names[37] = "Assembly text"
    elif changed_field == "copper-type":
        board.layer_types[4] = "signal"
    else:
        board.page = "A3"
    current = analyze(reopened, review_snapshot(board, module))
    assert current.sections == initial.sections
    with pytest.raises(ValidationError, match="changed"):
        validate_review(reopened, current)


def test_context_fingerprinting_is_read_only_and_ignores_property_order() -> None:
    """Serialize detached state and canonicalize maps without dirtying native input."""
    board = ContextBoard()
    original = deepcopy(board.__dict__)
    module = native_module(board)
    first = snapshot_board(board, module)
    assert board.__dict__ == original
    board.properties = dict(reversed(tuple(board.properties.items())))
    board.settings["plot_directory"] = "second"
    second = snapshot_board(board, module)
    assert first == second
    assert board.font_cache_changed is False


@pytest.mark.parametrize("location", ["board", "footprint"])
@pytest.mark.parametrize(
    ("field", "changed_value"),
    [("point", "9 8"), ("filled", False), ("needs_refill", True)],
)
def test_zone_pixels_and_readiness_change_capture_revision_and_revoke_approval(
    location: str,
    field: str,
    changed_value: Any,
) -> None:
    """A cached plot cannot outlive a board or footprint zone's current fill state."""
    board, zone = ContextBoard(), ContextZone()
    if location == "board":
        board.zones = (zone,)
    else:
        board.footprints = (ContextFootprint(zone),)
    module, config = native_module(board), review_config()
    initial = review_snapshot(board, module)
    assert review_snapshot(board, module) == initial
    analysis = analyze(config, initial)
    reviewed = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(row.section_id for row in analysis.sections),
    )
    validate_review(reviewed, analysis)
    original_value = getattr(zone, field)

    setattr(zone, field, changed_value)

    changed = review_snapshot(board, module)
    assert changed.traces == initial.traces
    assert changed.context_digest != initial.context_digest
    assert changed != initial  # CaptureSession uses this revision comparison.
    with pytest.raises(ValidationError, match="changed"):
        validate_review(reviewed, analyze(reviewed, changed))
    setattr(zone, field, original_value)
    board.settings["plot_directory"] = "unrelated-export-directory"
    assert review_snapshot(board, module) == initial


def test_zone_readiness_is_bound_to_each_zone_not_a_sorted_flag_multiset() -> None:
    """Two zones exchanging readiness states still change the capture revision."""
    board = ContextBoard()
    first, second = ContextZone(), ContextZone()
    second.point = "9 8"
    first.needs_refill = True
    board.zones = (first, second)
    module = native_module(board)
    initial = snapshot_board(board, module)

    first.needs_refill, second.needs_refill = False, True

    assert snapshot_board(board, module).context_digest != initial.context_digest
    first.needs_refill, second.needs_refill = True, False
    board.zones = (second, first)
    assert snapshot_board(board, module) == initial


def test_unreadable_rendered_settings_fail_closed_with_actionable_error() -> None:
    """Do not approve an image context when a native title-block read fails."""
    board = ContextBoard()

    def broken_title() -> Any:
        """Model a failing native getter, not an absent optional legacy API."""
        raise RuntimeError("native title read failed")

    board.GetTitleBlock = broken_title
    with pytest.raises(
        BoardSnapshotError, match="rendered board settings.*native title read failed"
    ):
        snapshot_board(board, native_module(board))
