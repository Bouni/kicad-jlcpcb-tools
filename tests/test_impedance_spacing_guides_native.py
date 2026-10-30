"""Native KiCad 10 review of the three normalized user-reported geometries.

These owned boards reproduce the portable copper coordinates. They do not read
or modify the user's PCB. Older native footprint support remains covered by the
separate KiCad 7–10 adapter suite.
"""

from collections import Counter
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile

import pytest

from impedance import model, review_tracking, service
from impedance.database import ImpedanceDatabase
from impedance.matching import analyze
from impedance.palette import read_theme_context
from impedance.pcbnew_adapter import snapshot_board
from impedance.repository import ImpedanceRepository
from impedance.spacing_checks import compare_pair_spacing, measure_pair_spacing
from impedance.spacing_review import spacing_review_entries
from scripts.linux_native_impedance_smoke import (
    configure_palette,
    event,
    modal,
    stock_button,
    wait_for_preview,
    write_json,
)
from scripts.native_impedance_workflow import (
    FixtureBoardProvider,
    _SpacingReportFields,
    approve_spacing_review,
    check_spacing_guide_pixels,
    check_spacing_report,
    wait_for_saved_capture_restore,
    wait_until,
)
from tests.impedance_spacing_regression_fixtures import reported_spacing_geometry
from tests.native_kicad_support import native_bindings, requires_native_plotting
from tests.native_wx_support import isolated_native

pytestmark = [
    pytest.mark.native_kicad,
    pytest.mark.kicad10_impedance,
    requires_native_plotting,
]
__all__ = ["native_bindings"]
_OFFSET = (40_000_000, 40_000_000)
_PAIR = ("USB_D+", "USB_D-")
_TARGET = 202_300


def _reported_native_board(pcbnew: Any, name: str, path: Path) -> Any:
    """Construct actual native tracks and footprint pads at the reported geometry."""
    geometry = reported_spacing_geometry(name)
    board = pcbnew.BOARD()
    board.SetFileName(str(path))
    board.SetCopperLayerCount(2)
    nets = {}
    for canonical, native_name in zip(("P", "N"), _PAIR):
        net = pcbnew.NETINFO_ITEM(board, native_name)
        board.Add(net)
        nets[canonical] = net
    for trace in geometry.traces:
        assert len(trace.points) == 2
        item = pcbnew.PCB_TRACK(board)
        item.SetStart(
            pcbnew.VECTOR2I(*(a + b for a, b in zip(trace.points[0], _OFFSET)))
        )
        item.SetEnd(pcbnew.VECTOR2I(*(a + b for a, b in zip(trace.points[1], _OFFSET))))
        item.SetWidth(trace.width_nm)
        item.SetLayer(pcbnew.F_Cu)
        item.SetNet(nets[trace.net])
        board.Add(item)
    footprints = {}
    for feature in geometry.copper_features:
        if feature.footprint_id not in footprints:
            footprint = pcbnew.FOOTPRINT(board)
            footprint.SetReference(feature.footprint_id)
            footprint.SetValue("Reported spacing fixture")
            footprint.Reference().SetVisible(False)
            footprint.Value().SetVisible(False)
            board.Add(footprint)
            footprints[feature.footprint_id] = footprint
        footprint = footprints[feature.footprint_id]
        pad = pcbnew.PAD(footprint)
        pad.SetNumber(feature.pad_number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetSize(pcbnew.VECTOR2I(feature.size_x_nm, feature.size_y_nm))
        pad.SetShape(
            pcbnew.PAD_SHAPE_CIRCLE
            if feature.shape == "circle"
            else pcbnew.PAD_SHAPE_ROUNDRECT
        )
        if feature.shape == "roundrect":
            pad.SetRoundRectRadiusRatio(
                feature.corner_radius_nm / min(feature.size_x_nm, feature.size_y_nm)
            )
        pad.SetOrientationDegrees(feature.rotation_deg)
        pad.SetPosition(
            pcbnew.VECTOR2I(*(a + b for a, b in zip(feature.center, _OFFSET)))
        )
        pad.SetNet(nets[feature.net])
        footprint.Add(pad)
    assert pcbnew.SaveBoard(str(path), board)
    return board


def _check_reported_native_geometry(snapshot: Any, board: Any, name: str) -> Any:
    """Assert user-observable outcomes and authoritative native pad identities."""
    source = reported_spacing_geometry(name)
    native_footprints = {
        str(fp.m_Uuid.AsString()): str(fp.GetReference())
        for fp in board.GetFootprints()
    }
    assert len(snapshot.copper_features) == len(source.copper_features)
    reverse_net = dict(zip(_PAIR, ("P", "N")))
    assert {
        (native_footprints[pad.footprint_id], pad.pad_number, reverse_net[pad.net])
        for pad in snapshot.copper_features
    } == {(pad.footprint_id, pad.pad_number, pad.net) for pad in source.copper_features}
    check = compare_pair_spacing(measure_pair_spacing(snapshot, _PAIR, "F.Cu"), _TARGET)
    assert check.coverage == "complete"
    assert check.transition_length_nm > (
        20_000_000 if name == "testpoints_with_terminal" else 5_000_000
    )
    if name == "row27":
        assert check.verdict == "match" and not check.issues
    else:
        assert check.verdict == "deviation" and check.issues
        members = tuple(member for issue in check.issues for member in issue.members)
        assert members
        if name == "connector":
            assert all(
                point[0] - _OFFSET[0] > 1_000_000
                for member in members
                for point in member.source_points
            )
        else:
            assert any(
                issue.gap_nm
                and issue.gap_nm.lower_nm <= 331_300 <= issue.gap_nm.upper_nm
                for issue in check.issues
            )
            assert all(
                point[0] - _OFFSET[0] < -1_000_000 or point[0] - _OFFSET[0] > 3_000_000
                for member in members
                for point in member.source_points
            )
    return check


def _check_native_combined_measurement_pixels(
    entry: Any, capture: Any, wx: Any
) -> None:
    """Native font labels and all three metric colors survive PNG composition."""
    image = wx.Image(BytesIO(capture.data), wx.BITMAP_TYPE_PNG)
    assert image.IsOk()
    assert (image.GetWidth(), image.GetHeight()) == (entry.width_px, entry.height_px)
    x, y, width, height = entry.composition.metrics_rect
    pixels = memoryview(image.GetDataBuffer())
    counts = [0, 0, 0]
    for row in range(y, y + height):
        for column in range(x, x + width):
            offset = (row * image.GetWidth() + column) * 3
            red, green, blue = pixels[offset : offset + 3]
            counts[0] += min(red, green, blue) > 220
            counts[1] += red < 180 and green > 200 and blue > 200
            counts[2] += red > 200 and green > 160 and blue < 160
    assert all(count > 100 for count in counts), counts


@pytest.mark.parametrize(
    ("name", "light_palette"),
    [
        ("row27", False),
        ("connector", False),
        ("testpoints_with_terminal", False),
        ("testpoints_with_terminal", True),
    ],
)
def test_native_reported_spacing_guides_approval_save_reopen_and_export(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    name: str,
    light_palette: bool,
) -> None:
    """Real guide PNGs receive individual consent and never enter the vendor XLSX."""
    if isolated_native(
        request, "KICAD_IMPEDANCE_REPORTED_GUIDES_CHILD", native="kicad"
    ):
        return
    # Worker-owned persisted palettes are read by the real native plotter. The
    # light case proves cyan/white guides retain their dark contrast keylines.
    configure_palette(tmp_path / "native-palette", custom=light_palette)
    bindings = request.getfixturevalue("native_bindings")
    from impedance.dialog import ImpedanceDialog
    from impedance.integration import CaptureSession

    pcbnew, wx, app = bindings.pcbnew, bindings.wx, bindings.app
    board = _reported_native_board(pcbnew, name, tmp_path / f"{name}.kicad_pcb")
    provider = FixtureBoardProvider(pcbnew, board)
    snapshot = snapshot_board(board, provider)
    check = _check_reported_native_geometry(snapshot, board, name)
    source_bytes = Path(board.GetFileName()).read_bytes()
    spec = model.Specification(
        "reported",
        name,
        "90",
        "differential",
        "Default",
        (model.LayerSettings("F.Cu", ("B.Cu",), _TARGET),),
    )
    config = model.Config(True, (spec,))
    entries = spacing_review_entries(
        config, snapshot, analyze(config, snapshot).sections
    )
    assert bool(entries) is (name != "row27")
    assert len({entry.view_id for entry in entries}) == len(entries)
    database = ImpedanceDatabase(tmp_path / "jlcpcb/project.db")
    repository = ImpedanceRepository(
        database, database.resolve_board(board.GetFileName())
    )
    controls = SimpleNamespace(
        parent=SimpleNamespace(pcbnew=provider),
        _selected_output_variant=lambda: None,
        _snapshot=lambda: snapshot_board(board, provider),
    )
    captures = []
    approvals = []
    reviewed_hashes = {}

    def open_dialog(saved: Any, directory: str, persist: Any = None) -> Any:
        """Use the production capture/cache contracts in a real dialog constructor."""
        target = tmp_path / directory
        target.mkdir()
        capture = CaptureSession(controls, target)
        captures.append(capture)
        return ImpedanceDialog(
            None,
            saved,
            capture.snapshot(),
            preview=capture.preview,
            refresh_snapshot=capture.snapshot,
            verify_snapshot=capture.verify,
            verify_captures=capture.verify_captures,
            verify_spacing_captures=capture.verify_spacing_captures,
            prepare_capture=capture.prepare,
            appearance_context=lambda: (read_theme_context(provider), None),
            fetch_stackup_catalog=lambda *_args, **_kwargs: (),
            save_config=persist,
        )

    def persist(saved: Any) -> None:
        """Persist only through the actual Save handler's repository callback."""
        assert repository.save(saved, 0) == 1

    main = open_dialog(config, "initial", persist)

    def review() -> None:
        """Paint every required entry and click the real individual approval buttons."""
        pane = main.preview_pane
        wait_for_preview(app, pane)
        for index in range(len(pane.sections)):
            pane.select(index)
            wait_for_preview(app, pane)
        assert not main.session.approved
        for entry in main.session.spacing_entries:
            pane.select(pane.entries.index(entry))
            wait_for_preview(app, pane)
            app.Yield()
            before = len(main.session.pending_spacing_reviews)
            reviewed_hashes[entry.view_id] = pane._capture.sha256
            approval = approve_spacing_review(
                app,
                wx,
                main,
                main.spacing_approve_button,
                entry,
                verify_guides=True,
                capture_path=tmp_path / f"{name}-guided-review.png"
                if not approvals
                else None,
            )
            approvals.append(approval)
            assert len(main.session.pending_spacing_reviews) == before - 1
            assert approval["guide_png_sha256"] == reviewed_hashes[entry.view_id]
        wait_until(
            app, main.approve_button.IsEnabled, "Reviewed rows did not enable approval"
        )
        event(wx, main.approve_button, wx.EVT_BUTTON)
        assert main.session.approved
        event(wx, stock_button(main, wx.ID_OK), wx.EVT_BUTTON)

    try:
        assert modal(wx, main, review) == wx.ID_OK
        restored, revision = repository.load()
        assert revision == 1 and restored == main.config
        history = restored.review_tracking.spacing_images
        assert len(history) == len(entries)
        database_before = database.path.read_bytes()
        captures[0].close()
        reopened = open_dialog(restored, "cold-reopen")

        def inspect_reopened() -> None:
            """Cold native PNG verification may restore proof, but cannot restamp consent."""
            wait_for_saved_capture_restore(app, reopened)
            assert reopened.session.approved
            assert not reopened.session.pending_spacing_reviews
            assert reopened.session.config.review_tracking.spacing_images == history
            assert all(
                reopened.session.spacing_capture_digests[entry.view_id]
                == review_tracking.spacing_capture_fingerprint(
                    entry.input_digest, reviewed_hashes[entry.view_id]
                )
                for entry in reopened.session.spacing_entries
            )
            event(wx, stock_button(reopened, wx.ID_CANCEL), wx.EVT_BUTTON)

        assert modal(wx, reopened, inspect_reopened) == wx.ID_CANCEL
        assert database.path.read_bytes() == database_before
        assert repository.load() == (restored, revision)
        plan = service.prepare(restored, snapshot, 2)
        assert plan is not None
        output = tmp_path / "export"
        output.mkdir()
        artifacts = service.export_reports(plan, board, provider, output)
        markup = artifacts.html_report.read_text()
        check_spacing_report(
            markup,
            restored,
            snapshot,
            plan.sections,
            require_spacing_reviews=bool(entries),
            workbook_bytes=artifacts.workbook.read_bytes(),
        )
        parser = _SpacingReportFields()
        parser.feed(markup)
        assert len(parser.spacing_cards) == len(entries)
        assert len(parser.rows) == len(plan.sections)
        for entry, card in zip(entries, parser.spacing_cards):
            capture = service.CapturedImage.from_bytes(card["data"])
            assert capture.sha256 == reviewed_hashes[entry.view_id]
            check_spacing_guide_pixels(entry, capture, wx)
            _check_native_combined_measurement_pixels(entry, capture, wx)
        with ZipFile(artifacts.workbook) as workbook:
            media = [
                workbook.read(n)
                for n in workbook.namelist()
                if n.startswith("xl/media/")
            ]
        assert Counter(media) == Counter(parser.workbook_images)
        assert not {hashlib.sha256(data).hexdigest() for data in media}.intersection(
            reviewed_hashes.values()
        )
        assert snapshot_board(board, provider) == snapshot
        assert Path(board.GetFileName()).read_bytes() == source_bytes
        write_json(
            tmp_path / f"{name}-spacing-evidence.json",
            {
                "case": name,
                "light_palette": light_palette,
                "verdict": check.verdict,
                "coverage": check.coverage,
                "required_spacing_images": len(entries),
                "native_pad_identities_verified": len(snapshot.copper_features),
                "actual_large_approvals": approvals,
                "save_reopen_exact_history_preserved": True,
                "guide_pngs_exactly_match_html": True,
                "diagnostic_media_absent_from_xlsx": True,
                "native_board_unchanged": True,
            },
        )
    finally:
        for capture in captures:
            capture.close()
