"""Prove budget evidence survives phase transitions and isolated-test failures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import impedance_native_metrics as api


@pytest.fixture(autouse=True)
def strict_native_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep helper-unit expectations independent of the selected native lane."""
    monkeypatch.delenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", raising=False)


def test_native_profile_defaults_to_strict_and_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent profile never silently grants the hosted-CI allowance."""
    assert api.native_budget_profile() == "strict"
    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", "relaxed")
    with pytest.raises(ValueError, match="native budget profile"):
        api.native_budget_profile()


@pytest.mark.parametrize("profile,multiplier", [("strict", 1), ("ci", 2)])
@pytest.mark.parametrize("cold,limit", [(True, 0.450), (False, 0.250)])
def test_explicit_native_profile_applies_to_each_opening_boundary(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    multiplier: int,
    cold: bool,
    limit: float,
) -> None:
    """Each CI opening has an inclusive 2x ceiling without pooling samples."""
    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", profile)
    sample = {"cold": cold, "open_seconds": limit * multiplier}
    api.assert_opening_budgets([sample])
    with pytest.raises(AssertionError):
        api.assert_opening_budgets(
            [sample, {"cold": cold, "open_seconds": limit * multiplier + 0.000001}]
        )


@pytest.mark.parametrize("profile,multiplier", [("strict", 1), ("ci", 2)])
@pytest.mark.parametrize(
    "measurement",
    ["save_close_seconds", "pending_save_seconds", "background_event_gap_seconds"],
)
def test_native_interaction_profile_checks_every_sample_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    multiplier: int,
    measurement: str,
) -> None:
    """Save, blocked Save, and dispatch gaps keep their individual maxima."""
    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", profile)
    sample = {measurement: 0.250 * multiplier}
    api.assert_operation_budgets([sample], measurement)
    with pytest.raises(AssertionError):
        api.assert_operation_budgets(
            [sample, {measurement: 0.250 * multiplier + 0.000001}], measurement
        )


def test_optional_operations_do_not_hide_missing_required_evidence() -> None:
    """Drafts may omit pending Save; completed Saves must still be recorded."""
    api.assert_operation_budgets([{}], "pending_save_seconds", optional=True)
    with pytest.raises(KeyError):
        api.assert_operation_budgets([{}], "save_close_seconds")
    with pytest.raises(AssertionError, match="require measurement samples"):
        api.assert_operation_budgets([], "save_close_seconds")
    with pytest.raises(AssertionError, match="require measurement samples"):
        api.assert_opening_budgets([])


@pytest.mark.parametrize("profile", ["strict", "ci"])
def test_fresh_native_process_environment_preserves_explicit_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """Plain-Python first-use children receive the same profile as their parent."""
    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", profile)
    monkeypatch.setenv("NATIVE_METRICS_UNIT_SENTINEL", "preserved")
    environment = api.native_budget_environment()
    assert environment["KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE"] == profile
    assert environment["NATIVE_METRICS_UNIT_SENTINEL"] == "preserved"
    environment["NATIVE_METRICS_UNIT_SENTINEL"] = "child-only"
    assert api.os.environ["NATIVE_METRICS_UNIT_SENTINEL"] == "preserved"


def test_ci_dump_retains_every_raw_sample_and_reports_strict_target_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing CI ceiling must still expose missed desktop targets."""
    monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", "ci")
    metrics = api.NativeMetrics()
    metrics.samples = [
        {"cold": True, "open_seconds": 0.450, "save_close_seconds": 0.017},
        {"cold": False, "open_seconds": 0.296, "save_close_seconds": 0.029},
        {"cold": False, "open_seconds": 0.291, "save_close_seconds": 0.018},
    ]
    properties: list[tuple[str, object]] = []
    metrics.dump(lambda name, value: properties.append((name, value)))
    samples = properties[0][1]
    assert samples is metrics.samples and len(samples) == 3
    assert max(sample["open_seconds"] for sample in metrics.samples) == 0.450
    for sample in metrics.samples:
        assert sample["native_budget_profile"] == "ci"
        assert sample["native_budget_multiplier"] == 2
        assert sample["native_budget_limits_seconds"]["save_close_seconds"] == 0.500
    assert metrics.samples[0]["strict_target_misses"] == {}
    for sample, seconds in zip(metrics.samples[1:], (0.296, 0.291)):
        assert sample["native_budget_limits_seconds"]["open_seconds"] == 0.500
        assert sample["strict_target_misses"] == {
            "open_seconds": {"seconds": seconds, "target_seconds": 0.250}
        }


@pytest.mark.parametrize(
    "cold,seconds,accepted",
    [
        (True, 0.450, True),
        (True, 0.450001, False),
        (False, 0.250, True),
        (False, 0.250001, False),
    ],
)
def test_opening_budget_includes_only_the_applicable_boundary(
    cold: bool, seconds: float, accepted: bool
) -> None:
    """Accept exact limits and reject slower first-use or warm openings."""
    samples = [{"cold": cold, "open_seconds": seconds}]
    if accepted:
        api.assert_opening_budgets(samples)
    else:
        with pytest.raises(AssertionError):
            api.assert_opening_budgets(samples)


def test_slow_warm_opening_cannot_borrow_the_first_use_budget() -> None:
    """A permissible cold opening cannot relax the limit of any later opening."""
    cold = {"cold": True, "open_seconds": 0.300}
    api.assert_opening_budgets([cold])
    with pytest.raises(AssertionError):
        api.assert_opening_budgets([cold, {"cold": False, "open_seconds": 0.300}])


def test_every_fresh_process_receives_the_first_use_opening_budget() -> None:
    """Process sample indices do not turn cold openings into warm ones."""
    api.assert_opening_budgets(
        [
            {"cold": True, "fresh_process_sample": index, "open_seconds": 0.450}
            for index in range(3)
        ]
    )


def test_opening_ends_only_after_current_visible_source_checked_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paint request or loaded PNG cannot satisfy the usable-opening budget."""
    clock = [10.0]
    monkeypatch.setattr(api, "perf_counter", lambda: clock[0])
    metrics = api.NativeMetrics()
    metrics.begin()
    canvas = SimpleNamespace(_recorded=False)
    preview = SimpleNamespace(canvas=canvas, displayed=False)
    dialog = SimpleNamespace(
        preview_pane=preview,
        IsShownOnScreen=lambda: True,
        _row_ids=("selected",),
        session=SimpleNamespace(analysis=object()),
        _capture_restore_pending=True,
    )
    clock[0] = 10.1
    metrics.record_open(dialog, canvas)
    assert "open_seconds" not in metrics.sample
    canvas._recorded = True
    metrics.record_open(dialog, canvas)
    assert "open_seconds" not in metrics.sample
    preview.displayed = True
    metrics.record_open(dialog, SimpleNamespace(_recorded=True))
    assert "open_seconds" not in metrics.sample
    metrics.record_open(dialog, canvas)
    assert metrics.sample["open_seconds"] == pytest.approx(0.1)
    assert metrics.sample["approval_verification_pending_at_open"] is True
    assert metrics.phase == "background"
    clock[0] = 20.0
    metrics.record_open(dialog, canvas)
    assert metrics.sample["open_seconds"] == pytest.approx(0.1)


def test_function_durations_keep_entry_phase_and_exception_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later work and exceptions cannot reclassify or discard an earlier call."""
    clock = iter([1.0, 2.0, 5.0, 6.0, 8.0])
    monkeypatch.setattr(api, "perf_counter", lambda: next(clock))
    metrics = api.NativeMetrics()
    metrics.begin()

    def transition() -> str:
        metrics.phase = "background"
        return "real return"

    assert metrics.timed("transition", transition)() == "real return"
    metrics.sample["opening_functions"] = metrics.summary("opening")

    def fail() -> None:
        raise ValueError("real failure")

    with pytest.raises(ValueError, match="real failure"):
        metrics.timed("later_work", fail)()
    assert metrics.sample["opening_functions"] == {
        "transition": {"calls": 1, "inclusive_seconds": 3.0, "max_seconds": 3.0}
    }
    assert metrics.summary("background") == {
        "later_work": {"calls": 1, "inclusive_seconds": 2.0, "max_seconds": 2.0}
    }


def test_isolated_failure_preserves_child_evidence_in_parent_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child budget failures still expose phase timings in the parent JUnit."""
    properties: list[tuple[str, object]] = []
    samples = [{"open_seconds": 0.3, "opening_functions": {"matching": {"calls": 1}}}]
    monkeypatch.delenv("KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT", raising=False)

    def child(_request: Any, _marker: str, *, native: str) -> bool:
        assert native == "kicad"
        artifact = Path(api.os.environ["KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT"])
        artifact.write_text(json.dumps(samples), encoding="utf-8")
        raise AssertionError("250 ms opening budget")

    monkeypatch.setattr(api, "isolated_native", child)
    with pytest.raises(AssertionError, match="250 ms opening budget"):
        api.isolate_with_metrics(
            SimpleNamespace(),
            monkeypatch,
            lambda name, value: properties.append((name, value)),
            "TEST_METRICS_CHILD",
            native="kicad",
        )
    assert properties == [("native_performance_samples", samples)]
    assert "KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT" not in api.os.environ


@pytest.mark.parametrize("profile", ["strict", "ci"])
def test_isolated_native_process_receives_selected_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """The pytest child sees explicit selection even when strict is the default."""
    if profile == "ci":
        monkeypatch.setenv("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE", profile)

    def child(_request: Any, _marker: str, *, native: str) -> bool:
        assert native == "wx"
        assert api.os.environ["KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE"] == profile
        return True

    monkeypatch.setattr(api, "isolated_native", child)
    assert api.isolate_with_metrics(
        SimpleNamespace(), monkeypatch, lambda _name, _value: None, "TEST_PROFILE_CHILD"
    )
    assert api.os.environ.get("KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE") == (
        profile if profile == "ci" else None
    )


def test_partial_sample_dump_retains_failed_operation_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Evidence is written before budget assertions, even without completed Save."""
    artifact = tmp_path / "child.json"
    monkeypatch.setenv("KICAD_IMPEDANCE_PERFORMANCE_ARTIFACT", str(artifact))
    metrics = api.NativeMetrics()
    metrics.begin()
    metrics.timed("constructor", lambda: None)()
    metrics.dump(lambda _name, _value: None)
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["cold"] is True
    assert saved[0]["phase_functions"]["opening"]["constructor"]["calls"] == 1
    assert "save_close_seconds" not in saved[0]
