"""Test-only timing and isolated-process evidence for native impedance budgets."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Optional

import pytest

from tests.impedance_performance_fixtures import (
    FIRST_USE_BUDGET_SECONDS,
    PERFORMANCE_BUDGET_SECONDS,
)
from tests.native_wx_support import isolated_native

_ARTIFACT_ENV = "KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT"
_PROFILE_ENV = "KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE"
_PROFILE_MULTIPLIERS = {"strict": 1, "ci": 2}
_INTERACTION_MEASUREMENTS = (
    "save_close_seconds",
    "pending_save_seconds",
    "background_event_gap_seconds",
)


def native_budget_profile() -> str:
    """Select an explicit native gate policy; local runs retain strict targets."""
    profile = os.environ.get(_PROFILE_ENV, "strict")
    if profile not in _PROFILE_MULTIPLIERS:
        raise ValueError(f"Unknown native budget profile: {profile!r}")
    return profile


def native_budget_environment() -> dict[str, str]:
    """Propagate the selected policy to fresh plain-Python measurement children."""
    return dict(os.environ, **{_PROFILE_ENV: native_budget_profile()})


def annotate_budget_evidence(samples: list[dict[str, Any]]) -> None:
    """Retain raw timings and disclose strict target misses under either profile."""
    profile = native_budget_profile()
    multiplier = _PROFILE_MULTIPLIERS[profile]
    for sample in samples:
        targets = {
            name: PERFORMANCE_BUDGET_SECONDS
            for name in _INTERACTION_MEASUREMENTS
            if name in sample
        }
        if "open_seconds" in sample:
            targets["open_seconds"] = (
                FIRST_USE_BUDGET_SECONDS
                if sample["cold"]
                else PERFORMANCE_BUDGET_SECONDS
            )
        sample.update(
            native_budget_profile=profile,
            native_budget_multiplier=multiplier,
            native_budget_limits_seconds={
                name: target * multiplier for name, target in targets.items()
            },
            strict_target_misses={
                name: {"seconds": sample[name], "target_seconds": target}
                for name, target in targets.items()
                if sample[name] > target
            },
        )


def assert_opening_budgets(samples: list[dict[str, Any]]) -> None:
    """Check each first-use or warm opening against its own inclusive limit."""
    assert samples, "Native timing gates require measurement samples"
    multiplier = _PROFILE_MULTIPLIERS[native_budget_profile()]
    assert all(
        sample["open_seconds"]
        <= multiplier
        * (FIRST_USE_BUDGET_SECONDS if sample["cold"] else PERFORMANCE_BUDGET_SECONDS)
        for sample in samples
    ), samples


def assert_operation_budgets(
    samples: list[dict[str, Any]], measurement: str, *, optional: bool = False
) -> None:
    """Check every interaction sample, allowing absent pending-Save measurements."""
    assert samples, "Native timing gates require measurement samples"
    limit = PERFORMANCE_BUDGET_SECONDS * _PROFILE_MULTIPLIERS[native_budget_profile()]
    assert all(
        sample[measurement] <= limit
        for sample in samples
        if not optional or measurement in sample
    ), samples


def isolate_with_metrics(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    marker: str,
    *,
    native: str = "wx",
) -> bool:
    """Keep child-process measurements in the parent JUnit even on failure."""
    if os.environ.get(marker) == "1":
        return False
    with TemporaryDirectory(prefix="impedance-native-metrics-") as directory:
        artifact = Path(directory) / "samples.json"
        with monkeypatch.context() as patch:
            patch.setenv(_ARTIFACT_ENV, str(artifact))
            patch.setenv(_PROFILE_ENV, native_budget_profile())
            try:
                return isolated_native(request, marker, native=native)
            finally:
                if artifact.exists():
                    record_property(
                        "native_performance_samples",
                        json.loads(artifact.read_text(encoding="utf-8")),
                    )


class NativeMetrics:
    """Measure real operations without adding state or hooks to product code.

    Function durations are inclusive: parent and child timings overlap. Each
    operation belongs to its phase at entry; background work cannot inflate the
    completed opening summary. No per-cell native operations are instrumented.
    """

    def __init__(self) -> None:
        self.samples: list[dict[str, Any]] = []
        self.phase: Optional[str] = None
        self.started = 0.0
        self.timings: dict[str, dict[str, list[float]]] = {}

    def begin(self) -> None:
        """Start immediately before the public operation, including cold work."""
        self.timings = {}
        self.samples.append({"sample": len(self.samples), "cold": not self.samples})
        self.phase = "opening"
        self.started = perf_counter()

    @property
    def sample(self) -> dict[str, Any]:
        """Return the active sample, including partial evidence on failures."""
        return self.samples[-1]

    def timed(self, name: str, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap one existing operation, retaining its return and exception behavior."""

        @wraps(callback)
        def measured(*args: Any, **kwargs: Any) -> Any:
            phase = self.phase
            if phase is None:
                return callback(*args, **kwargs)
            started = perf_counter()
            try:
                return callback(*args, **kwargs)
            finally:
                self.timings.setdefault(phase, {}).setdefault(name, []).append(
                    perf_counter() - started
                )

        return measured

    def summary(self, phase: str) -> dict[str, dict[str, object]]:
        """Report phase-local counts and inclusive times, never mixed totals."""
        return {
            name: {
                "calls": len(values),
                "inclusive_seconds": sum(values),
                "max_seconds": max(values),
            }
            for name, values in self.timings.get(phase, {}).items()
        }

    def record_open(self, dialog: Any, canvas: Any) -> None:
        """Stop at the source-checked, actually painted selected-image callback."""
        if (
            self.phase != "opening"
            or "open_seconds" in self.sample
            or canvas is not dialog.preview_pane.canvas
            or not canvas._recorded
            or not dialog.preview_pane.displayed
            or not dialog.IsShownOnScreen()
            or not dialog._row_ids
            or dialog.session.analysis is None
        ):
            return
        self.sample.update(
            open_seconds=perf_counter() - self.started,
            opening_functions=self.summary("opening"),
            approval_verification_pending_at_open=dialog._capture_restore_pending,
            selected_image_source_checked_and_painted=True,
            workbook_rows=len(dialog._row_ids),
        )
        self.phase = "background"

    def dump(self, record_property: Callable[[str, object], None]) -> None:
        """Persist before assertions so failed budgets retain complete evidence."""
        if self.samples:
            self.sample["phase_functions"] = {
                phase: self.summary(phase) for phase in self.timings
            }
        annotate_budget_evidence(self.samples)
        record_property("native_performance_samples", self.samples)
        artifact = os.environ.get(_ARTIFACT_ENV)
        if artifact:
            Path(artifact).write_text(
                json.dumps(self.samples, indent=2), encoding="utf-8"
            )
