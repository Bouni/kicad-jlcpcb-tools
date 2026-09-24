"""Representative routed workbooks shared by Python and isolated native budgets.

Fixture construction and historical approval happen outside measured operations.
The routes model native track items: each segment has its own stable identity;
arcs retain their native-arc marker and three centerline samples.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from impedance.model import BoardSnapshot, Config, Section

CLASS_COUNT = 4
NETS_PER_CLASS = 3
SEGMENTS_PER_ROUTE = 32
PERFORMANCE_BUDGET_SECONDS = 0.250
FIRST_USE_BUDGET_SECONDS = 0.450
REPEATED_SAMPLES = 3


@dataclass(frozen=True)
class RoutedWorkbook:
    """Immutable inputs independent of native window and filesystem ownership."""

    snapshot: BoardSnapshot
    draft: Config
    approved: Config


def routed_draft(api: SimpleNamespace) -> tuple[BoardSnapshot, Config]:
    """Create representative inputs without importing UI or approving captures."""
    model = api.model
    traces = []
    features = []
    memberships = []
    classes = tuple(f"Controlled{index}" for index in range(CLASS_COUNT))
    for class_index, net_class in enumerate(classes):
        for net_index in range(NETS_PER_CLASS):
            route = class_index * NETS_PER_CLASS + net_index
            net = f"{net_class}_SIGNAL{net_index}"
            layer = "F.Cu" if net_index % 2 == 0 else "In2.Cu"
            y = route * 3_000_000
            width = 150_000 + net_index * 25_000
            memberships.append((net, (net_class,)))
            for segment in range(SEGMENTS_PER_ROUTE):
                start = (segment * 500_000, y)
                end = ((segment + 1) * 500_000, y)
                arc = segment % 8 == 3
                points = (
                    (start, (start[0] + 250_000, y + 80_000), end)
                    if arc
                    else (start, end)
                )
                traces.append(
                    model.Trace(
                        f"route-{route}-segment-{segment}",
                        layer,
                        net,
                        width,
                        points,
                        is_native_arc=arc,
                    )
                )
            traces.append(
                model.Trace(
                    f"route-{route}-pad-stub",
                    layer,
                    net,
                    width,
                    ((-300_000, y), (-200_000, y)),
                )
            )
            features.append(
                model.CopperFeature(
                    f"pad-{route}",
                    net,
                    layer,
                    "pad",
                    "rect",
                    (-250_000, y),
                    600_000,
                    700_000,
                )
            )
    for index in range(96):
        x = index % 16 * 1_000_000
        y = 40_000_000 + index // 16 * 1_000_000
        traces.append(
            model.Trace(
                f"ground-context-{index}",
                "F.Cu" if index % 2 == 0 else "In2.Cu",
                "GND",
                350_000,
                ((x, y), (x + 1_000_000, y + 200_000)),
            )
        )
    snapshot = model.BoardSnapshot(
        layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
        traces=tuple(traces),
        context_digest="representative-492-native-track-items",
        net_classes=("Default",) + classes,
        net_class_memberships=tuple(memberships) + (("GND", ("Default",)),),
        net_class_context_digest="four-controlled-classes-three-nets-each",
        copper_features=tuple(features),
        board_bounds=(-1_000_000, -1_000_000, 18_000_000, 47_000_000),
    )
    draft = model.Config(
        enabled=True,
        specifications=tuple(
            model.Specification(
                f"class-{index}",
                net_class,
                "50",
                "single_ended",
                net_class,
                (
                    model.LayerSettings("F.Cu", ("In1.Cu",)),
                    model.LayerSettings("In2.Cu", ("In1.Cu", "B.Cu")),
                ),
            )
            for index, net_class in enumerate(classes)
        ),
    )
    stackups = importlib.import_module(f"{api.model.__package__}.stackup_model")
    construction = stackups.Stackup(
        "representative-4-layer",
        "Representative four layer",
        4,
        "1.6",
        "1",
        "0.5",
        calculator_id="representative-4-layer",
    )
    draft = replace(
        draft,
        stackup=construction,
        width_results=tuple(
            stackups.WidthResult(
                spec.spec_id,
                settings.layer,
                stackups.calculation_fingerprint(construction, spec, settings.layer),
                "success",
                175_000,
                "2026-10-20T12:00:00.000000Z",
                model="microstrip" if settings.layer == "F.Cu" else "stripline",
                calculation_digest="a" * 64,
            )
            for spec in draft.specifications
            for settings in spec.layer_settings
        ),
    )
    return snapshot, draft


def routed_workbook(api: SimpleNamespace, image_sha256: str) -> RoutedWorkbook:
    """Add genuine saved approval history to the representative draft inputs."""
    snapshot, draft = routed_draft(api)
    return RoutedWorkbook(
        snapshot,
        draft,
        historical_approval(api, snapshot, draft, lambda _s: image_sha256),
    )


def historical_approval(
    api: SimpleNamespace,
    snapshot: BoardSnapshot,
    draft: Config,
    image_hash: Callable[[Section], str],
) -> Config:
    """Seed genuine saved history from deterministic or freshly native-rendered pixels."""
    session = api.dialog.ReviewSession(draft, snapshot)
    analysis = session.refresh()
    session.approve()
    tracking = importlib.import_module(f"{api.dialog.__package__}.review_tracking")
    source = tracking.source_fingerprint(snapshot)
    specs = {spec.spec_id: spec for spec in draft.specifications}
    timestamp = "2026-10-20T12:00:00.000000Z"
    included = tuple(
        section
        for section in analysis.sections
        if section.section_id in session.included
    )
    images = tuple(
        tracking.ImageView(
            section.spec_id,
            section.layer,
            section.section_id,
            tracking.capture_fingerprint(
                source, specs[section.spec_id], section, image_hash(section)
            ),
            timestamp,
        )
        for section in included
    )
    layers = tuple(
        tracking.LayerApproval(
            spec.spec_id,
            settings.layer,
            tracking.layer_fingerprint(
                source,
                spec,
                settings.layer,
                analysis.sections,
                stackup=draft.stackup,
                width_results=draft.width_results,
            ),
            timestamp,
            tuple(
                (image.section_id, image.capture_digest)
                for image in images
                if (image.spec_id, image.layer) == (spec.spec_id, settings.layer)
            ),
        )
        for spec in draft.specifications
        for settings in spec.layer_settings
    )
    approved = replace(
        session.config, review_tracking=tracking.ReviewTracking(images, layers)
    )
    # This is genuine persisted history, not a parallel test-only approved flag.
    assert api.model.Config.from_dict(approved.to_dict()) == approved
    return approved


def populate_native_board(
    board: Any, pcbnew: Any, workbook: RoutedWorkbook, path: Path
) -> None:
    """Populate an owned disposable KiCad board without replacing live project state.

    Native calls follow the KiCad 10 SWIG contracts for NET_SETTINGS.SetNetclass,
    SetNetclassPatternAssignment, PCB_ARC, and ZONE. This helper runs only in the
    explicitly enabled isolated native child; its ownership comes from empty_board.
    """
    board.SetFileName(str(path))
    board.SetCopperLayerCount(4)
    settings = board.GetDesignSettings().m_NetSettings
    for spec in workbook.draft.specifications:
        settings.SetNetclass(spec.net_class, pcbnew.NETCLASS(spec.net_class))
        settings.SetNetclassPatternAssignment(f"{spec.net_class}_*", spec.net_class)
    nets = {}
    for name, _classes in workbook.snapshot.net_class_memberships:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net
    for trace in workbook.snapshot.traces:
        item = pcbnew.PCB_ARC(board) if trace.is_native_arc else pcbnew.PCB_TRACK(board)
        item.SetStart(pcbnew.VECTOR2I(*trace.points[0]))
        item.SetEnd(pcbnew.VECTOR2I(*trace.points[-1]))
        if trace.is_native_arc:
            item.SetMid(pcbnew.VECTOR2I(*trace.points[1]))
        item.SetWidth(trace.width_nm)
        item.SetLayer(board.GetLayerID(trace.layer))
        item.SetNet(nets[trace.net])
        board.Add(item)
    for index, feature in enumerate(workbook.snapshot.copper_features):
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference(f"TP{index}")
        footprint.SetPosition(pcbnew.VECTOR2I(*feature.center))
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
        board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(feature.size_x_nm, feature.size_y_nm))
        pad.SetPosition(pcbnew.VECTOR2I(*feature.center))
        layers = pcbnew.LSET()
        layers.AddLayer(board.GetLayerID(feature.layer))
        pad.SetLayerSet(layers)
        pad.SetNet(nets[feature.net])
        footprint.Add(pad)
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECT)
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetStart(pcbnew.VECTOR2I(-1_000_000, -1_000_000))
    outline.SetEnd(pcbnew.VECTOR2I(18_000_000, 47_000_000))
    outline.SetWidth(50_000)
    board.Add(outline)
    for layer in ("In1.Cu", "B.Cu"):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(board.GetLayerID(layer))
        zone.SetNet(nets["GND"])
        corners = zone.Outline()
        corners.NewOutline()
        for x, y in (
            (-500_000, -500_000),
            (17_500_000, -500_000),
            (17_500_000, 46_500_000),
            (-500_000, 46_500_000),
        ):
            corners.Append(x, y)
        board.Add(zone)
    board.BuildConnectivity()
    pcbnew.PCB_IO_KICAD_SEXPR().SaveBoard(str(path), board)
