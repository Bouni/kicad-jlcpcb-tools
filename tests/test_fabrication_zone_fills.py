"""Regression tests for copper-zone refill and empty-pour reporting."""

import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
from unittest.mock import MagicMock, call
import uuid
from zipfile import ZipFile

import pytest

_ROOT = Path(__file__).parent.parent

_LAYER_ID_PROFILES = (
    pytest.param(
        {
            "F_Cu": 0,
            "In1_Cu": 1,
            "In2_Cu": 2,
            "B_Cu": 31,
            "B_Paste": 34,
            "F_Paste": 35,
            "B_SilkS": 36,
            "F_SilkS": 37,
            "B_Mask": 38,
            "F_Mask": 39,
            "Edge_Cuts": 44,
        },
        id="kicad-8",
    ),
    pytest.param(
        {
            "F_Cu": 0,
            "F_Mask": 1,
            "B_Cu": 2,
            "B_Mask": 3,
            "In1_Cu": 4,
            "F_SilkS": 5,
            "In2_Cu": 6,
            "B_SilkS": 7,
            "F_Paste": 13,
            "B_Paste": 15,
            "Edge_Cuts": 25,
        },
        id="kicad-9-plus",
    ),
)


@pytest.fixture(params=_LAYER_ID_PROFILES)
def zone_harness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> types.SimpleNamespace:
    """Load Fabrication with isolated KiCad mocks for one layer-ID profile."""
    pcbnew = types.ModuleType("pcbnew")
    constants = {
        "PLOT_FORMAT_GERBER": 1,
        "DRILL_MARKS_NO_DRILL_SHAPE": 0,
        **request.param,
    }
    for name, value in constants.items():
        setattr(pcbnew, name, value)

    copper_ids = {
        constants["F_Cu"],
        constants["In1_Cu"],
        constants["In2_Cu"],
        constants["B_Cu"],
    }
    pcbnew.IsCopperLayer = MagicMock(side_effect=copper_ids.__contains__)

    for name in (
        "EXCELLON_WRITER",
        "PCB_VIA",
        "PLOT_CONTROLLER",
        "VECTOR2I",
        "FromMM",
        "ToMM",
        "wxPoint",
    ):
        setattr(pcbnew, name, MagicMock(name=name))

    zone_filler = MagicMock(name="zone_filler")
    pcbnew.ZONE_FILLER = MagicMock(name="ZONE_FILLER", return_value=zone_filler)
    pcbnew.Refresh = MagicMock(name="Refresh")
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)

    package_name = f"_zone_fill_test_{uuid.uuid4().hex}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)

    footprint_helpers = types.ModuleType(f"{package_name}.footprint_helpers")
    footprint_helpers.get_is_dnp = MagicMock(return_value=False)
    monkeypatch.setitem(
        sys.modules,
        f"{package_name}.footprint_helpers",
        footprint_helpers,
    )

    module_name = f"{package_name}.fabrication"
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "fabrication.py")
    assert spec is not None and spec.loader is not None
    fabrication_module = importlib.util.module_from_spec(spec)
    fabrication_module.__package__ = package_name
    monkeypatch.setitem(sys.modules, module_name, fabrication_module)
    spec.loader.exec_module(fabrication_module)

    layer_names = {
        constants["F_Cu"]: "F.Cu",
        constants["In1_Cu"]: "In1.Cu",
        constants["In2_Cu"]: "In2.Cu",
        constants["B_Cu"]: "B.Cu",
        constants["F_Mask"]: "F.Mask",
        constants["Edge_Cuts"]: "Edge.Cuts",
    }
    return types.SimpleNamespace(
        Fabrication=fabrication_module.Fabrication,
        ArchiveEntry=fabrication_module.ArchiveEntry,
        constants=constants,
        layer_names=layer_names,
        pcbnew=pcbnew,
        zone_filler=zone_filler,
    )


def _make_zone(layers, areas, *, netname="GND", rule_area=False):
    """Return a zone mock with per-layer filled polygon areas."""
    zone = MagicMock(name=f"zone_{netname or 'no_net'}")
    zone.GetIsRuleArea.return_value = rule_area
    zone.GetLayerSet.return_value.Seq.return_value = list(layers)
    polygons = {}
    for layer, area in areas.items():
        polygon = MagicMock(name=f"filled_polygons_{layer}")
        polygon.Area.return_value = area
        polygons[layer] = polygon
    zone.GetFilledPolysList.side_effect = polygons.__getitem__
    zone.GetNetname.return_value = netname
    return zone


def _make_fabrication(harness, zones, settings=None):
    """Return a bare Fabrication instance and its board mock."""
    board = MagicMock(name="board")
    board.Zones.return_value = zones
    board.GetLayerName.side_effect = harness.layer_names.__getitem__

    fabrication = object.__new__(harness.Fabrication)
    fabrication.parent = types.SimpleNamespace(
        settings={} if settings is None else settings
    )
    fabrication.board = board
    fabrication.logger = MagicMock(name="logger")
    return fabrication, board


@pytest.mark.parametrize(
    "settings",
    [{}, {"gerber": {"fill_zones": True}}],
    ids=("default", "enabled"),
)
def test_fill_zones_refills_when_enabled_or_default(zone_harness, settings):
    """Enabled behavior refills before it inspects the resulting polygons."""
    front = zone_harness.constants["F_Cu"]
    zone = _make_zone([front], {front: 0})
    filled_polygons = zone.GetFilledPolysList(front)
    zone.GetFilledPolysList.reset_mock()
    zones = [zone]

    def mark_zone_filled(_zones):
        """Simulate KiCad replacing an empty fill with a positive-area fill."""
        filled_polygons.Area.return_value = 12

    zone_harness.zone_filler.Fill.side_effect = mark_zone_filled
    fabrication, board = _make_fabrication(zone_harness, zones, settings)

    assert fabrication.fill_zones() == []

    zone_harness.pcbnew.ZONE_FILLER.assert_called_once_with(board)
    zone_harness.zone_filler.Fill.assert_called_once_with(zones)
    zone_harness.pcbnew.Refresh.assert_called_once_with()
    zone.GetFilledPolysList.assert_called_once_with(front)


def test_fill_zones_disabled_checks_without_refilling(zone_harness):
    """Disabled refill still inspects existing fills without mutating the board."""
    front = zone_harness.constants["F_Cu"]
    zone = _make_zone([front], {front: 0}, netname="VCC")
    fabrication, _board = _make_fabrication(
        zone_harness,
        [zone],
        {"gerber": {"fill_zones": False}},
    )

    assert fabrication.fill_zones() == ["VCC on F.Cu"]
    zone_harness.pcbnew.ZONE_FILLER.assert_not_called()
    zone_harness.pcbnew.Refresh.assert_not_called()


def test_failed_native_fill_stops_before_inspecting_partial_copper(
    zone_harness: types.SimpleNamespace,
) -> None:
    """KiCad False is a failed fill, even if some copper polygons remain."""
    front = zone_harness.constants["F_Cu"]
    zone = _make_zone([front], {front: 12})
    fabrication, _board = _make_fabrication(zone_harness, [zone])
    zone_harness.zone_filler.Fill.return_value = False

    with pytest.raises(RuntimeError, match="(?i)fill"):
        fabrication.fill_zones()

    zone.GetFilledPolysList.assert_not_called()
    zone_harness.pcbnew.Refresh.assert_called_once_with()


@pytest.mark.parametrize("refill", [True, False])
def test_explicit_board_preparation_does_not_use_retained_fabrication_board(
    zone_harness: types.SimpleNamespace, refill: bool
) -> None:
    """Review may target a reloaded native board without changing output ownership."""
    front = zone_harness.constants["F_Cu"]
    original_zone = _make_zone([front], {front: 12})
    fabrication, original_board = _make_fabrication(
        zone_harness, [original_zone], {"gerber": {"fill_zones": refill}}
    )
    active_zone = _make_zone([front], {front: 0}, netname="ACTIVE_GND")
    active_board = MagicMock(name="active_board")
    active_board.Zones.return_value = [active_zone]
    active_board.GetLayerName.return_value = "Active copper"

    assert fabrication.fill_zones(active_board) == ["ACTIVE_GND on Active copper"]

    assert fabrication.board is original_board
    original_board.Zones.assert_not_called()
    original_board.GetLayerName.assert_not_called()
    original_zone.GetFilledPolysList.assert_not_called()
    active_zone.GetFilledPolysList.assert_called_once_with(front)
    active_board.GetLayerName.assert_called_once_with(front)
    if refill:
        zone_harness.pcbnew.ZONE_FILLER.assert_called_once_with(active_board)
        zone_harness.zone_filler.Fill.assert_called_once_with([active_zone])
    else:
        zone_harness.pcbnew.ZONE_FILLER.assert_not_called()


def test_fill_zones_reports_only_empty_copper(zone_harness):
    """Filled copper is omitted while empty copper is reported."""
    front = zone_harness.constants["F_Cu"]
    back = zone_harness.constants["B_Cu"]
    filled = _make_zone([front], {front: 12}, netname="GND")
    empty = _make_zone([back], {back: 0}, netname="VCC")
    fabrication, _board = _make_fabrication(zone_harness, [filled, empty])

    assert fabrication.fill_zones() == ["VCC on B.Cu"]
    filled.GetFilledPolysList.assert_called_once_with(front)
    empty.GetFilledPolysList.assert_called_once_with(back)


def test_fill_zones_ignores_rule_areas(zone_harness):
    """Rule areas are keepouts and must not be inspected as copper pours."""
    front = zone_harness.constants["F_Cu"]
    rule_area = _make_zone([front], {front: 0}, rule_area=True)
    fabrication, _board = _make_fabrication(zone_harness, [rule_area])

    assert fabrication.fill_zones() == []
    rule_area.GetLayerSet.assert_not_called()
    rule_area.GetFilledPolysList.assert_not_called()


def test_fill_zones_preserves_multilayer_zone_order(zone_harness):
    """Empty layers retain the order provided by the zone layer set."""
    front = zone_harness.constants["F_Cu"]
    inner = zone_harness.constants["In1_Cu"]
    back = zone_harness.constants["B_Cu"]
    zone = _make_zone(
        [back, inner, front],
        {back: 0, inner: 23, front: 0},
        netname="SIGNAL",
    )
    fabrication, _board = _make_fabrication(zone_harness, [zone])

    assert fabrication.fill_zones() == ["SIGNAL on B.Cu", "SIGNAL on F.Cu"]


def test_fill_zones_labels_unconnected_zones(zone_harness):
    """An empty zone without a net uses the stable ``no net`` label."""
    front = zone_harness.constants["F_Cu"]
    zone = _make_zone([front], {front: 0}, netname="")
    fabrication, _board = _make_fabrication(zone_harness, [zone])

    assert fabrication.fill_zones() == ["no net on F.Cu"]


def test_fill_zones_excludes_non_copper_layers(zone_harness):
    """Empty technical layers do not trigger a false copper-pour warning."""
    mask = zone_harness.constants["F_Mask"]
    front = zone_harness.constants["F_Cu"]
    technical_zone = _make_zone([mask], {mask: 0}, netname="GRAPHICS")
    copper_zone = _make_zone([front], {front: 0}, netname="GND")
    fabrication, _board = _make_fabrication(
        zone_harness,
        [technical_zone, copper_zone],
    )

    assert fabrication.fill_zones() == ["GND on F.Cu"]
    technical_zone.GetFilledPolysList.assert_not_called()
    copper_zone.GetFilledPolysList.assert_called_once_with(front)
    assert zone_harness.pcbnew.IsCopperLayer.call_args_list == [call(mask), call(front)]


def _make_output_fabrication(
    harness: types.SimpleNamespace, project: Path, board_name: str
) -> Any:
    """Create a real fabrication output layout using an isolated board mock."""
    board = MagicMock(name=board_name)
    board.GetFileName.return_value = str(project / board_name)
    return harness.Fabrication(types.SimpleNamespace(settings={}), board)


def test_same_project_boards_have_independent_plot_directories_and_archives(
    zone_harness: types.SimpleNamespace, tmp_path: Path
) -> None:
    """Two boards cannot include, overwrite, or republish each other's plot data."""
    first = _make_output_fabrication(zone_harness, tmp_path, "controller.kicad_pcb")
    second = _make_output_fabrication(zone_harness, tmp_path, "display.kicad_pcb")
    first_plots = Path(first.gerberdir)
    second_plots = Path(second.gerberdir)
    assert first_plots == tmp_path / "jlcpcb" / "gerber" / "controller"
    assert second_plots == tmp_path / "jlcpcb" / "gerber" / "display"
    (first_plots / "copper.gbr").write_bytes(b"controller copper")
    (second_plots / "copper.gbr").write_bytes(b"display copper")
    (first_plots.parent / "legacy.gbr").write_bytes(b"stale shared output")

    first_zip = first.zip_gerber_excellon()
    second_zip = second.zip_gerber_excellon()
    assert first_zip != second_zip
    with ZipFile(first_zip) as archive:
        assert archive.namelist() == ["copper.gbr"]
        assert archive.read("copper.gbr") == b"controller copper"
    with ZipFile(second_zip) as archive:
        assert archive.namelist() == ["copper.gbr"]
        assert archive.read("copper.gbr") == b"display copper"

    previous_second_zip = second_zip.read_bytes()
    (first_plots / "copper.gbr").write_bytes(b"updated controller copper")
    first.zip_gerber_excellon()
    assert second_zip.read_bytes() == previous_second_zip
    assert (second_plots / "copper.gbr").read_bytes() == b"display copper"


def test_fabrication_embeds_only_explicit_workbook_and_drops_it_when_disabled(
    zone_harness: types.SimpleNamespace, tmp_path: Path
) -> None:
    """Only this run's workbook joins manufacturing files in the published ZIP."""
    fabrication = _make_output_fabrication(zone_harness, tmp_path, "board.kicad_pcb")
    plots = Path(fabrication.gerberdir)
    (plots / "copper.gbr").write_bytes(b"copper")
    (plots / "holes.drl").write_bytes(b"drill")
    (plots / "map.pdf").write_bytes(b"map")
    (plots / "stale.xlsx").write_bytes(b"stale workbook")
    (plots / "snippet.png").write_bytes(b"temporary image")
    workbook = tmp_path / "Required_impedance_control.xlsx"
    workbook.write_bytes(b"current impedance workbook")

    zip_path = fabrication.zip_gerber_excellon(
        [zone_harness.ArchiveEntry(workbook, workbook.name)]
    )
    workbook.unlink()
    with ZipFile(zip_path) as archive:
        assert archive.namelist() == [
            "Required_impedance_control.xlsx",
            "copper.gbr",
            "holes.drl",
            "map.pdf",
        ]
        assert (
            archive.read("Required_impedance_control.xlsx")
            == b"current impedance workbook"
        )
        assert archive.testzip() is None

    fabrication.zip_gerber_excellon()
    with ZipFile(zip_path) as archive:
        assert archive.namelist() == ["copper.gbr", "holes.drl", "map.pdf"]
