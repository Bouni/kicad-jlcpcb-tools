"""Check empty-zone detail and navigation handlers without a desktop session."""

from collections.abc import Iterator
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any

import pytest

from copper_zones import EmptyCopperZone

from .wx_harness import load_siblings, module, wx_stubs


@pytest.fixture
def dialog_module() -> Iterator[ModuleType]:
    """Use production handlers with display-independent controls and rendering."""
    package = "_copper_zone_dialog_handlers"
    replacements = wx_stubs(Dialog=type("Dialog", (), {}))
    replacements.update(
        {
            f"{package}.copper_zone_preview": module(
                f"{package}.copper_zone_preview", ZonePreviewRenderer=object
            ),
            f"{package}.impedance.dialog_preview": module(
                f"{package}.impedance.dialog_preview", PreviewCanvas=object
            ),
        }
    )
    with load_siblings(package, ("copper_zone_dialog",), replacements) as loaded:
        yield loaded["copper_zone_dialog"]


class _Text:
    """Preserve text written by an event handler for subsequent reads."""

    def __init__(self) -> None:
        self.value = ""

    def SetValue(self, value: str) -> None:  # noqa: N802 -- wx control interface.
        """Replace the current detail text."""
        self.value = value

    def GetValue(self) -> str:  # noqa: N802 -- wx control interface.
        """Return the actual stored detail text."""
        return self.value

    def SetInsertionPoint(self, position: int) -> None:  # noqa: N802 -- wx control interface.
        """Retain the position used to reveal a new item's first line."""
        self.insertion_point = position


class _Button:
    """Retain enabled state across navigation handlers."""

    def __init__(self) -> None:
        self.enabled = True

    def Enable(self, enabled: bool) -> None:  # noqa: N802 -- wx control interface.
        """Apply the handler's requested state."""
        self.enabled = enabled


class _Preview:
    """Retain captures so failure paths can verify stale-image removal."""

    def __init__(self) -> None:
        self.capture = ""

    def clear(self) -> None:
        """Discard the currently visible capture."""
        self.capture = ""

    def set_capture(self, capture: str) -> None:
        """Display a renderer's selected-zone capture token."""
        self.capture = capture


def _dialog(runtime: ModuleType, regions: list[EmptyCopperZone]) -> Any:
    """Bind production navigation and error handlers to stateful control doubles."""
    selection = SimpleNamespace(row=0)
    selection.GetSelection = lambda: selection.row

    def render(region: EmptyCopperZone, *, zoom: bool = False) -> str:
        """Represent which selected geometry would be displayed."""
        if region.geometry_error:
            raise ValueError(region.geometry_error)
        return f"{region.zone_id}:{zoom}"

    dialog = SimpleNamespace(
        _regions=regions,
        _zoom=False,
        zones_list=selection,
        detail=_Text(),
        zoom_button=_Button(),
        overview_button=_Button(),
        preview=_Preview(),
        _renderer=SimpleNamespace(render=render),
        _renderer_error="",
        _wrap_text=lambda: None,
        Layout=lambda: None,
    )
    for name in (
        "_update_preview",
        "_preview_failed",
        "_on_selection",
        "_on_zoom",
        "_on_overview",
    ):
        setattr(
            dialog,
            name,
            MethodType(getattr(runtime.EmptyCopperZonesDialog, name), dialog),
        )
    dialog._update_preview()
    return dialog


def test_outline_dimensions_and_identity_survive_zoom_and_reopening(
    dialog_module: ModuleType,
) -> None:
    """An enlarged small item must still state its true dimensions and identity."""
    region = EmptyCopperZone(
        "teardrop-location",
        "VEXT",
        0,
        "F.Cu",
        (
            (
                (83_000_000, 66_000_000),
                (84_000_000, 66_000_000),
                (83_000_000, 66_300_000),
            ),
        ),
    )
    for _ in range(2):
        dialog = _dialog(dialog_module, [region])
        detail = dialog.detail.GetValue()
        assert "Outline dimensions: 1.000 × 0.300 mm" in detail
        assert "Zone ID: teardrop-location" in detail
        assert dialog.detail.insertion_point == 0
        dialog._on_zoom(None)
        assert dialog.detail.GetValue() == detail
        assert dialog.preview.capture == "teardrop-location:True"
        dialog._on_overview(None)
        assert dialog.detail.GetValue() == detail
        assert dialog.preview.capture == "teardrop-location:False"


def test_failed_selection_retains_its_location_and_discards_previous_image(
    dialog_module: ModuleType,
) -> None:
    """A failed second item must not leave the first item's plausible picture."""
    regions = [
        EmptyCopperZone(
            "first", "VEXT", 0, "F.Cu", (((0, 0), (1_000_000, 0), (0, 1_000_000)),)
        ),
        EmptyCopperZone(
            "broken",
            "VEXT",
            0,
            "F.Cu",
            (),
            "Outline read failed",
            (20_000_000, 10_000_000),
        ),
    ]
    dialog = _dialog(dialog_module, regions)
    assert dialog.preview.capture == "first:False"
    dialog._on_zoom(None)
    dialog.zones_list.row = 1
    dialog._on_selection(None)
    detail = dialog.detail.GetValue()
    assert "Zone ID: broken" in detail
    assert "Zone ID: first" not in detail
    assert "Outline dimensions: unavailable" in detail
    assert "Board X 20.000 mm, Y 10.000 mm" in detail
    assert not dialog._zoom
    assert not dialog.zoom_button.enabled
    assert dialog.preview.capture == ""
    assert "Outline read failed" in dialog._preview_error


def test_teardrop_detail_explains_type_name_and_refill_result(
    dialog_module: ModuleType,
) -> None:
    """A successful refill and a later stale fill have distinct explanations."""
    boundary = (((0, 0), (1_000_000, 0), (0, 300_000)),)
    region = EmptyCopperZone(
        "teardrop-id",
        "VEXT",
        0,
        "F.Cu",
        boundary,
        is_teardrop=True,
        zone_name="U31 pad 1",
        is_filled=True,
        needs_refill=False,
        refill_performed=True,
    )
    detail = _dialog(dialog_module, [region]).detail.GetValue()
    assert "VEXT on F.Cu — generated teardrop" in detail
    assert "Zone name: U31 pad 1" in detail
    assert "Zone ID: teardrop-id" in detail
    assert "after" in detail.lower() and "refill" in detail.lower()
    assert "Outline dimensions: 1.000 × 0.300 mm" in detail

    stale = EmptyCopperZone(
        "teardrop-id",
        "VEXT",
        0,
        "F.Cu",
        boundary,
        is_teardrop=True,
        is_filled=False,
        needs_refill=True,
        refill_performed=False,
    )
    detail = _dialog(dialog_module, [stale]).detail.GetValue()
    assert "disabled" in detail.lower()
    assert "after" not in detail.lower()
    assert "required" in detail.lower()
