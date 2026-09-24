"""Keep exact segment clearance while avoiding unnecessary projection work."""

import math
import random

import pytest

from impedance import regions
from impedance.model import Point


@pytest.mark.parametrize(
    ("point", "end", "expected"),
    [
        ((-3, 4), (10, 0), 5.0),
        ((13, 4), (10, 0), 5.0),
        ((0, 4), (10, 0), 4.0),
        ((10, 4), (10, 0), 4.0),
        ((5, 7), (10, 0), 7.0),
        ((3, 4), (0, 0), 5.0),
    ],
    ids=["before", "after", "start", "end", "interior", "degenerate"],
)
def test_point_distance_does_not_clamp_a_known_projection(
    monkeypatch: pytest.MonkeyPatch, point: Point, end: Point, expected: float
) -> None:
    """Endpoint and interior cases select their geometry without min/max clamps."""
    clamps = []

    def minimum(left: float, right: float) -> float:
        """Observe actual scalar work without replacing its return value."""
        clamps.append("minimum")
        return min(left, right)

    def maximum(left: float, right: float) -> float:
        """Observe actual scalar work without replacing its return value."""
        clamps.append("maximum")
        return max(left, right)

    monkeypatch.setattr(regions, "min", minimum, raising=False)
    monkeypatch.setattr(regions, "max", maximum, raising=False)
    assert regions._point_distance(point, (0, 0), end) == expected
    assert not clamps


def test_point_distance_retains_exact_projection_results_for_native_coordinates() -> (
    None
):
    """Compare native-range geometry with the independent clamped-vector formula."""
    generator = random.Random(250)
    for case in range(1_000):
        point, start, end = tuple(
            tuple(generator.randrange(-(2**31), 2**31) for _ in range(2))
            for _ in range(3)
        )
        if case % 10 == 0:
            end = start
        dx, dy = end[0] - start[0], end[1] - start[1]
        denominator = dx * dx + dy * dy
        fraction = (
            max(
                0.0,
                min(
                    1.0,
                    ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
                    / denominator,
                ),
            )
            if denominator
            else 0.0
        )
        expected = math.hypot(
            point[0] - start[0] - fraction * dx,
            point[1] - start[1] - fraction * dy,
        )
        assert regions._point_distance(point, start, end) == expected
