"""Keep spacing-review readiness observable without optional desktop bindings."""

from collections.abc import Callable
from dataclasses import dataclass
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from . import test_impedance_spacing_controls_native as spacing


@dataclass
class Rectangle:
    """Supply only immutable native rectangle observations, without wx imports."""

    values: tuple[int, int, int, int]

    def Get(self) -> tuple[int, int, int, int]:
        """Return the complete bounds consumed by the native assertion."""
        return self.values


class Size(NamedTuple):
    """Preserve wx size's tuple access and named dimensions in paint fixtures."""

    width: int
    height: int


@pytest.fixture
def native_paint() -> Any:
    """Deliver explicitly partial/full paint events to a stateful native window."""
    state = SimpleNamespace(width=207, height=540, painted_width=1)
    handlers, skipped = [], []
    window = SimpleNamespace(
        Bind=lambda _kind, handler: handlers.append(handler),
        Unbind=lambda _kind, handler: handlers.remove(handler),
        Refresh=lambda: None,
        GetPosition=lambda: (0, 496),
        GetClientSize=lambda: Size(state.width, state.height),
        GetUpdateRegion=lambda: SimpleNamespace(
            Contains=lambda size: 2 if state.painted_width >= size.width else 1
        ),
    )
    wx = SimpleNamespace(
        Platform="__WXGTK__", EVT_PAINT=1, InRegion=2, Rect=lambda _point, size: size
    )

    def paint() -> None:
        for handler in tuple(handlers):
            handler(SimpleNamespace(Skip=lambda: skipped.append(True)))

    return SimpleNamespace(
        state=state,
        window=window,
        wx=wx,
        handlers=handlers,
        skipped=skipped,
        paint=paint,
    )


def viewer(frame: tuple[int, int, int, int], *, maximized: bool = False) -> Any:
    """Expose a stateful native-sized frame and separately delivered client bounds."""
    state = SimpleNamespace(
        frame=frame, client=(1, 20, 1920, 1061), maximized=maximized
    )
    controls = {
        "canvas": (9, 61, 1904, 945),
        "caption": (9, 28, 1904, 17),
        "status": (9, 1014, 1904, 17),
        "approve_button": (9, 1039, 210, 34),
        "cancel": (1700, 1039, 100, 34),
    }
    state.IsMaximized = lambda: state.maximized
    state.GetScreenRect = lambda: Rectangle(state.frame)
    state.ClientToScreen = lambda _point: SimpleNamespace(
        x=state.client[0], y=state.client[1]
    )
    state.GetClientSize = lambda: SimpleNamespace(
        width=state.client[2], height=state.client[3]
    )
    for name, bounds in controls.items():
        setattr(
            state,
            name,
            SimpleNamespace(
                GetScreenRect=lambda bounds=bounds: Rectangle(bounds),
                IsShownOnScreen=lambda: True,
            ),
        )
    state.FindWindow = lambda _identifier: state.cancel
    state.GetChildren = lambda: [getattr(state, name) for name in controls]
    return state


def test_restore_waits_for_complete_native_geometry_after_state_flag_changes() -> None:
    """The reported normal flag plus lagging maximized geometry is not ready."""
    state = viewer((0, 0, 1922, 1086))
    area = (0, 0, 1920, 1080)
    assert not spacing._review_geometry_ready(state, area, maximized=False)
    # Frame decorations and client size arrive separately from the state bit.
    state.frame = (0, 0, 1920, 1080)
    assert not spacing._review_geometry_ready(state, area, maximized=False)
    state.client = (1, 20, 1918, 1059)
    assert spacing._review_geometry_ready(state, area, maximized=False)
    state.maximized = True
    assert not spacing._review_geometry_ready(state, area, maximized=False)


def test_geometry_readiness_preserves_control_and_client_containment() -> None:
    """A settled frame cannot hide a clipped review button or canvas."""
    state = viewer((0, 0, 1920, 1080))
    state.client = (1, 20, 1918, 1059)
    state.approve_button.GetScreenRect = lambda: Rectangle((1900, 1039, 210, 34))
    assert not spacing._review_geometry_ready(
        state, (0, 0, 1920, 1080), maximized=False
    )


def test_maximized_border_exception_still_requires_visible_client_content() -> None:
    """Preserve the native maximized-border allowance without allowing clipped content."""
    state = viewer((-1, -20, 1922, 1100), maximized=True)
    state.client = (0, 0, 1920, 1080)
    area = (0, 0, 1920, 1080)
    assert spacing._review_geometry_ready(state, area, maximized=True)
    state.client = (1, 20, 1920, 1061)
    assert not spacing._review_geometry_ready(state, area, maximized=True)


def test_footer_measurement_waits_for_current_full_native_paint(
    monkeypatch: pytest.MonkeyPatch,
    native_paint: Any,
) -> None:
    """Cached footer bounds and three Yield passes do not prove native allocation."""
    state = native_paint.state
    state.width, state.height = 720, 102

    def deliver(_wx: Any, predicate: Callable[[], bool], **_kwargs: Any) -> None:
        assert not predicate(), "Cached bounds cannot certify a fresh native paint"
        native_paint.paint()
        assert not predicate(), "A partial paint must not certify the whole footer"
        state.painted_width = state.width
        native_paint.paint()
        assert predicate()
        state.width = 792
        assert not predicate(), "Another requested layout needs another native paint"
        state.painted_width = state.width
        native_paint.paint()
        assert predicate()

    monkeypatch.setattr(spacing, "wait_until", deliver)
    spacing._wait_specification_footer_paint(native_paint.wx, native_paint.window)
    assert len(native_paint.skipped) == 3, (
        "Geometry was measured before a full native allocation paint; repeated Yield is insufficient"
    )
    assert not native_paint.handlers


@pytest.mark.parametrize("target", ["footer"])
def test_missing_native_paint_times_out_and_releases_observer(
    monkeypatch: pytest.MonkeyPatch,
    native_paint: Any,
    target: str,
) -> None:
    """A missing native paint fails instead of approving cached geometry."""
    assert target == "footer"

    def expire(_wx: Any, predicate: Callable[[], bool], **_kwargs: Any) -> None:
        assert not predicate()
        raise AssertionError("native allocation deadline")

    monkeypatch.setattr(spacing, "wait_until", expire)
    with pytest.raises(AssertionError, match="native allocation deadline"):
        spacing._wait_specification_footer_paint(native_paint.wx, native_paint.window)
    assert not native_paint.handlers


@pytest.mark.parametrize(
    "wait",
    [spacing._wait_specification_footer_paint],
    ids=["footer"],
)
def test_cocoa_does_not_acquire_a_gtk_paint_requirement(
    wait: Callable[..., None],
) -> None:
    """Keep the existing Cocoa geometry fixture lifecycle unchanged."""
    wait(SimpleNamespace(Platform="__WXMAC__"), object())


def test_helpers_import_without_optional_desktop_or_board_bindings() -> None:
    """These committed regressions also run in the ordinary, non-native lane."""
    code = (
        "import builtins\n"
        "original=builtins.__import__\n"
        "def without_native(name,*args,**kwargs):\n"
        " if name.split('.')[0] in {'wx','pcbnew'}: raise ModuleNotFoundError(name)\n"
        " return original(name,*args,**kwargs)\n"
        "builtins.__import__=without_native\n"
        "from tests.test_impedance_spacing_controls_native import _review_geometry_ready\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
