"""Optional native arc geometry failures must preserve existing render snapshots."""

from types import SimpleNamespace
from typing import Any

import pytest

from impedance.pcbnew_adapter import snapshot_board


@pytest.mark.parametrize("getter", ["GetPosition", "GetRadius"])
@pytest.mark.parametrize(
    "error_type", [AttributeError, TypeError, RuntimeError, OverflowError, ValueError]
)
def test_optional_effective_arc_getter_failure_preserves_render_geometry(
    getter: str, error_type: type[Exception]
) -> None:
    """Failure of spacing-only getters leaves a readable, unverified native arc."""

    def fail() -> Any:
        """Represent an expected unsupported native getter on an older binding."""
        raise error_type("unsupported effective geometry")

    item = SimpleNamespace(
        GetClass=lambda: "PCB_ARC",
        GetLayer=lambda: 0,
        GetWidth=lambda: 100_000,
        GetStart=lambda: SimpleNamespace(x=1_000_000, y=0),
        GetEnd=lambda: SimpleNamespace(x=0, y=1_000_000),
        GetMid=lambda: SimpleNamespace(x=600_000, y=800_000),
        GetPosition=lambda: SimpleNamespace(x=0, y=0),
        GetRadius=lambda: 1_000_000.0,
        GetNetname=lambda: "P",
        m_Uuid=SimpleNamespace(AsString=lambda: "native"),
    )
    setattr(item, getter, fail)
    board = SimpleNamespace(
        GetTracks=lambda: (item,),
        GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: (0, 31)),
        GetFootprints=lambda: (),
        GetDrawings=lambda: (),
    )
    trace = snapshot_board(board, SimpleNamespace(F_Cu=0, B_Cu=31)).traces[0]
    assert trace.is_native_arc
    assert trace.points[0] == (1_000_000, 0)
    assert trace.points[-1] == (0, 1_000_000)
    assert trace.native_arc_midpoint == (600_000, 800_000)
    assert trace.native_arc_center is None
    assert trace.native_arc_radius_nm is None
