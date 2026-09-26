"""Exercise class intent through reviewed rows, real XLSX writing and ZIP packaging."""

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import posixpath
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from fabrication_archive import ArchiveEntry, build_archive
from impedance import html_report, service, workbook
from impedance.matching import analyze, preview_sections
from impedance.model import BoardSnapshot, Config, LayerSettings, Section, Specification
from tests.rf_impedance_fixtures import COPPER_LAYERS, RF_CASES, RFCase
from tests.test_impedance_workbook import _NS, _png


def _class_plan(case: RFCase) -> service.ExportPlan:
    """Prepare one class covering all real fixture widths and physical layers."""
    traces = case.traces()
    class_name = "RF controlled"
    specification = Specification(
        spec_id="class-intent",
        label=case.title,
        target_ohms=case.target_ohms,
        kind=case.kind,
        net_class=class_name,
        layer_settings=tuple(
            LayerSettings(
                leg.layer, leg.reference_layers, leg.spacing_nm, leg.ground_gap_nm
            )
            for leg in {
                profile.layer: profile for profile in case.signal_profiles
            }.values()
        ),
    )
    snapshot = BoardSnapshot(
        COPPER_LAYERS,
        traces,
        "native-fixture-context",
        net_classes=(class_name,),
        net_class_memberships=tuple(
            (net, (class_name,)) for net in sorted({trace.net for trace in traces})
        ),
        net_class_context_digest="class-constraints",
        differential_pairs=(case.net_names,) if case.paired else (),
    )
    config = Config(enabled=True, specifications=(specification,))
    analysis = analyze(config, snapshot)
    config = replace(
        config,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )
    # Reopening the persisted payload must preserve the same approved rows.
    reopened = Config.from_dict(config.to_dict())
    plan = service.prepare(reopened, snapshot, layer_count=6)
    assert plan is not None
    return plan


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_netclass_export_resolves_each_layers_fields_in_real_workbook_and_archive(
    case: RFCase, tmp_path: Path
) -> None:
    """Top/inner/bottom geometry and both inner references reach actual XLSX cells."""
    plan = _class_plan(case)
    assert "section_groups" not in plan.config.to_dict()
    # Before gaps/target are entered, the editor must show the exact future rows,
    # not a layer-wide union or an independently computed bounding rectangle.
    draft = replace(plan.config.specifications[0], target_ohms="", layer_settings=())
    previewed = tuple(
        section
        for layer in plan.snapshot.layers
        for section in preview_sections(draft, plan.snapshot, layer)
    )
    assert previewed == plan.sections
    rendered: list[Section] = []
    written: list[service.ReportRow] = []
    images: dict[str, bytes] = {}

    class Renderer:
        """Use valid test PNGs; this test verifies output orchestration, not plotting."""

        def __init__(self) -> None:
            self.closed = False

        def render(
            self, section: Section, destination: Path, width_px: int, height_px: int
        ) -> Path:
            """Record exactly the sections approved after reopening."""
            assert not self.closed
            assert (width_px, height_px) == (800, 420)
            rendered.append(section)
            # Distinct real PNG bytes make wrong-row/reused-image wiring visible.
            color = bytes((len(rendered), 80, 160))
            images[section.section_id] = _png(pixel_data=(b"\x00" + color * 8) * 4)
            destination.write_bytes(images[section.section_id])
            return destination

        def close(self) -> None:
            """End native resource scope before XLSX generation."""
            assert not self.closed
            self.closed = True

    renderer = Renderer()

    def factory(board: Any, module: Any) -> Renderer:
        """Supply a resource whose release remains observable to the writer."""
        return renderer

    def writer(rows: Sequence[service.ReportRow], destination: Path) -> Path:
        """Run the actual vendor-template writer with per-layer resolved values."""
        assert renderer.closed
        written.extend(rows)
        return workbook.write_workbook(rows, destination)

    artifacts = service.export_reports(
        plan, object(), object(), tmp_path, factory, writer
    )
    output = artifacts.workbook
    assert rendered == list(plan.sections)
    assert len(written) == len(plan.sections)
    assert {
        trace.trace_id for section in plan.sections for trace in section.traces
    } == {trace.trace_id for trace in case.traces()}
    physical = {name: f"L{index}" for index, name in enumerate(COPPER_LAYERS, 1)}
    legs = {(leg.layer, leg.width_nm): leg for leg in case.signal_profiles}
    for section, row in zip(plan.sections, written):
        leg = legs[(section.layer, section.width_nm)]
        assert row.physical_signal_layer == physical[leg.layer]
        assert row.physical_reference_layers == tuple(
            physical[layer] for layer in leg.reference_layers
        )
        assert row.width_nm == leg.width_nm
        assert row.spacing_nm == leg.spacing_nm
        assert row.ground_gap_nm == leg.ground_gap_nm
        assert row.target_ohms == ("90" if case.paired else "50")
        assert row.kind == case.kind
        assert len(section.net_names) == (2 if case.paired else 1)

    with ZipFile(output) as package:
        assert package.testzip() is None
        sheet = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))  # noqa: S314
        namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for number, row in enumerate(written, 2):
            assert (
                sheet.findtext(f'.//s:c[@r="A{number}"]/s:is/s:t', namespaces=namespace)
                == row.physical_signal_layer
            )
            assert sheet.findtext(
                f'.//s:c[@r="B{number}"]/s:is/s:t', namespaces=namespace
            ) == ", ".join(row.physical_reference_layers)
            assert (
                sheet.findtext(f'.//s:c[@r="G{number}"]/s:v', namespaces=namespace)
                == row.target_ohms
            )
            expected_kind = ("Differential Pair" if case.paired else "Single Ended") + (
                " (Coplanar)" if case.coplanar else " (Non coplanar)"
            )
            assert (
                sheet.findtext(f'.//s:c[@r="C{number}"]/s:is/s:t', namespaces=namespace)
                == expected_kind
            )
            actual_width = Decimal(
                sheet.findtext(f'.//s:c[@r="D{number}"]/s:v', namespaces=namespace)
            )
            assert abs(actual_width - Decimal(row.width_nm) / 25400) < Decimal(
                "0.000001"
            )
            pair_gap, ground_gap = row.spacing_nm, row.ground_gap_nm
            if pair_gap is not None and ground_gap is not None:
                actual = sheet.findtext(
                    f'.//s:c[@r="F{number}"]/s:is/s:t', namespaces=namespace
                )
                values = dict(line.split(": ") for line in actual.splitlines())
                assert set(values) == {"Pair", "Ground"}
                assert abs(
                    Decimal(values["Pair"]) - Decimal(pair_gap) / 25400
                ) < Decimal("0.000001")
                assert abs(
                    Decimal(values["Ground"]) - Decimal(ground_gap) / 25400
                ) < Decimal("0.000001")
            elif pair_gap is not None or ground_gap is not None:
                actual = sheet.findtext(
                    f'.//s:c[@r="F{number}"]/s:v', namespaces=namespace
                )
                assert abs(
                    Decimal(actual) - Decimal(pair_gap or ground_gap) / 25400
                ) < Decimal("0.000001")
            else:
                assert list(sheet.find(f'.//s:c[@r="F{number}"]', namespace)) == []
        assert len(
            [name for name in package.namelist() if name.startswith("xl/media/")]
        ) == len(written)
        drawing = ET.fromstring(package.read("xl/drawings/drawing1.xml"))  # noqa: S314
        relations = ET.fromstring(package.read("xl/drawings/_rels/drawing1.xml.rels"))  # noqa: S314
        targets = {node.get("Id"): node.get("Target") for node in relations}
        anchored_rows = set()
        for anchor in drawing.findall("x:oneCellAnchor", _NS):
            row_index = int(anchor.findtext("x:from/x:row", namespaces=_NS))
            assert anchor.findtext("x:from/x:col", namespaces=_NS) == "7"
            assert row_index not in anchored_rows
            anchored_rows.add(row_index)
            embed = anchor.find("x:pic/x:blipFill/a:blip", _NS).get(
                f"{{{_NS['r']}}}embed"
            )
            target = targets[embed]
            image_path = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl/drawings", target))
            )
            assert (
                package.read(image_path)
                == images[plan.sections[row_index - 1].section_id]
            )
        assert anchored_rows == set(range(1, len(plan.sections) + 1))

    gerber = tmp_path / "board-F_Cu.gbr"
    gerber.write_bytes(b"G04 test Gerber*\nM02*\n")
    archive_path = tmp_path / "board-gerbers.zip"
    build_archive(
        archive_path,
        (
            ArchiveEntry(gerber, gerber.name),
            ArchiveEntry(output, output.name),
            ArchiveEntry(artifacts.html_report, artifacts.html_report.name),
        ),
    )
    with ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            gerber.name,
            workbook.OUTPUT_FILENAME,
            html_report.OUTPUT_FILENAME,
        }
        assert archive.read(workbook.OUTPUT_FILENAME) == output.read_bytes()
        assert (
            archive.read(html_report.OUTPUT_FILENAME)
            == artifacts.html_report.read_bytes()
        )
    disabled = replace(plan.config, enabled=False)
    assert service.prepare(disabled, plan.snapshot, 6) is None
    build_archive(archive_path, (ArchiveEntry(gerber, gerber.name),))
    with ZipFile(archive_path) as archive:
        assert archive.namelist() == [gerber.name]
