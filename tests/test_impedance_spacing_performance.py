"""Bound real differential geometry work and prohibit repeated export preflight work."""

from collections.abc import Callable
from dataclasses import replace
import gc
import json
import math
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import pytest

from impedance import model, service, spacing_checks
from impedance.matching import analyze
from tests.impedance_performance_fixtures import (
    CLASS_COUNT,
    NETS_PER_CLASS,
    PERFORMANCE_BUDGET_SECONDS,
    REPEATED_SAMPLES,
    SEGMENTS_PER_ROUTE,
    differential_routed_draft,
    differential_terminal_draft,
)

_WORKER_CASES = {
    "routed": REPEATED_SAMPLES,
    "terminal-match": REPEATED_SAMPLES,
    "terminal-deviation": REPEATED_SAMPLES,
    "work-limit": 1,
}


def _isolated_spacing_samples(
    case: str, record_property: Callable[[str, object], None]
) -> None:
    """Exclude earlier GUI doubles' cyclic garbage without changing the budget.

    Each worker retains the original complete sample loop and normal GC. The
    long-lived native opening/first-use/Save budgets remain separate tests.
    """
    assert case in _WORKER_CASES, case
    command = [
        sys.executable,
        "-m",
        "tests.test_impedance_spacing_performance",
        "--spacing-worker",
        case,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"Spacing worker timed out: {error.stdout!r} {error.stderr!r}")
    assert result.returncode == 0, (result.stdout, result.stderr)
    try:
        evidence = json.loads(result.stdout)
    except (TypeError, ValueError):
        pytest.fail(f"Invalid spacing evidence: {result.stdout!r} {result.stderr!r}")
    assert isinstance(evidence, dict), evidence
    assert evidence.get("case") == case, evidence
    policy = evidence.get("gc_before")
    assert isinstance(policy, dict) and policy.get("enabled") is True, evidence
    thresholds = policy.get("threshold")
    assert isinstance(thresholds, list) and len(thresholds) == 3, evidence
    assert all(type(value) is int and value >= 0 for value in thresholds), evidence
    assert evidence.get("gc_after") == policy, evidence
    samples = evidence.get("samples")
    assert isinstance(samples, list) and len(samples) == _WORKER_CASES[case], evidence
    assert all(
        type(value) in (int, float) and math.isfinite(value) and value > 0
        for value in samples
    ), evidence
    record_property("spacing_benchmark", result.stdout.strip())
    assert max(samples) <= PERFORMANCE_BUDGET_SECONDS, evidence


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{}",
        "[]",
        '{"case":"terminal-deviation","samples":[0.1,0.1,0.1]}',
        '{"case":"routed","samples":[0.1,0.1,0.1]}\n{}',
        *(
            json.dumps(
                {
                    "case": "routed",
                    "samples": samples,
                    "gc_before": {"enabled": True, "threshold": [700, 10, 10]},
                    "gc_after": {"enabled": True, "threshold": [700, 10, 10]},
                }
            )
            for samples in (
                [],
                [0.1, 0.1],
                [0.1, 0.1, 0.1, 0.1],
                [float("nan"), 0.1, 0.1],
                [float("inf"), 0.1, 0.1],
                [-0.1, 0.1, 0.1],
                [True, 0.1, 0.1],
                [0.3, 0.1, 0.1],
                [0.1, 0.3, 0.1],
                [0.1, 0.1, 0.3],
            )
        ),
    ],
)
def test_spacing_worker_rejects_incomplete_or_slow_evidence(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """A successful exit cannot conceal missing, filtered or slow samples."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        _isolated_spacing_samples("routed", lambda *_args: None)


@pytest.mark.parametrize("returncode", [1, -9])
def test_spacing_worker_failure_cannot_become_a_pass(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """Functional failures and worker crashes retain their diagnostic output."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, returncode, "partial samples", "worker failed"
        ),
    )
    with pytest.raises(AssertionError, match="worker failed"):
        _isolated_spacing_samples("routed", lambda *_args: None)


def test_spacing_worker_timeout_cannot_become_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung worker fails once and exposes both partial output streams."""

    def timed_out(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], 30, "partial samples", "worker hung")

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(pytest.fail.Exception, match="worker hung"):
        _isolated_spacing_samples("routed", lambda *_args: None)


def test_spacing_worker_keeps_exact_samples_and_process_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep one inherited-environment worker per case and publish its raw evidence."""
    evidence = {
        "case": "terminal-deviation",
        "samples": [0.201, 0.199, 0.202],
        "gc_before": {"enabled": True, "threshold": [700, 10, 10]},
        "gc_after": {"enabled": True, "threshold": [700, 10, 10]},
    }
    calls = []
    properties = []

    def completed(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(evidence), "")

    monkeypatch.setattr(subprocess, "run", completed)
    _isolated_spacing_samples(
        "terminal-deviation", lambda name, value: properties.append((name, value))
    )
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "tests.test_impedance_spacing_performance",
                "--spacing-worker",
                "terminal-deviation",
            ],
            {
                "cwd": Path(__file__).resolve().parents[1],
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 30,
            },
        )
    ]
    assert properties == [("spacing_benchmark", json.dumps(evidence))]
    with pytest.raises(AssertionError):
        _isolated_spacing_samples("unknown-case", lambda *_args: None)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "after",
    [
        {"enabled": False, "threshold": [700, 10, 10]},
        {"enabled": True, "threshold": [700_000, 10, 10]},
    ],
)
def test_spacing_worker_rejects_changed_gc_policy(
    monkeypatch: pytest.MonkeyPatch, after: dict[str, Any]
) -> None:
    """Neither disabling GC nor increasing thresholds can satisfy the budget."""
    evidence = {
        "case": "routed",
        "samples": [0.1, 0.1, 0.1],
        "gc_before": {"enabled": True, "threshold": [700, 10, 10]},
        "gc_after": after,
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(evidence), ""
        ),
    )
    with pytest.raises(AssertionError):
        _isolated_spacing_samples("routed", lambda *_args: None)


def test_differential_fixture_retains_many_native_sized_track_segments() -> None:
    """Keep measurement budgets from shrinking to a single ideal pair."""
    snapshot, config = differential_routed_draft(SimpleNamespace(model=model))
    assert len(snapshot.traces) == CLASS_COUNT * NETS_PER_CLASS * 2 * SEGMENTS_PER_ROUTE
    assert len(snapshot.differential_pairs) == CLASS_COUNT * NETS_PER_CLASS
    assert len(config.specifications) == CLASS_COUNT
    assert len({trace.width_nm for trace in snapshot.traces}) == 3
    assert {trace.layer for trace in snapshot.traces} == {"F.Cu", "In2.Cu"}
    assert config.stackup is None
    assert not config.width_results


def test_spacing_context_measures_each_pair_layer_once_across_target_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target-only changes compare cached geometry instead of measuring it again."""
    snapshot, config = differential_routed_draft(SimpleNamespace(model=model))
    analysis = analyze(config, snapshot)
    calls = []
    original = spacing_checks.measure_pair_spacing

    def measured(*args: Any, **kwargs: Any) -> Any:
        calls.append((args[1], args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(spacing_checks, "measure_pair_spacing", measured)
    context = spacing_checks.SpacingContext(snapshot)
    before = snapshot
    checks = spacing_checks.spacing_checks_for_sections(
        config, snapshot, analysis.sections, context=context
    )
    assert checks
    assert all(check.verdict == "match" for check in checks.values())
    assert len(calls) == len(snapshot.differential_pairs)
    edited = replace(
        config,
        specifications=tuple(
            replace(
                spec,
                layer_settings=tuple(
                    replace(settings, spacing_nm=200_000)
                    for settings in spec.layer_settings
                ),
            )
            for spec in config.specifications
        ),
    )
    changed = spacing_checks.spacing_checks_for_sections(
        edited, snapshot, analysis.sections, context=context
    )
    assert all(check.verdict == "deviation" for check in changed.values())
    assert len(calls) == len(snapshot.differential_pairs)
    assert snapshot == before


def test_segmented_differential_spacing_maximum_250ms(
    record_property: Callable[[str, object], None],
) -> None:
    """Retain every cold-context wall sample in one independent worker."""
    _isolated_spacing_samples("routed", record_property)


def _routed_spacing_samples() -> list[float]:
    """Measure independent cold contexts; retain every real monotonic sample."""
    snapshot, config = differential_routed_draft(SimpleNamespace(model=model))
    analysis = analyze(config, snapshot)
    samples = []
    for _ in range(REPEATED_SAMPLES):
        context = spacing_checks.SpacingContext(snapshot)
        started = perf_counter()
        checks = spacing_checks.spacing_checks_for_sections(
            config, snapshot, analysis.sections, context=context
        )
        samples.append(perf_counter() - started)
        assert checks
        assert all(
            check.verdict == "match" and check.coverage == "complete"
            for check in checks.values()
        )
    return samples


@pytest.mark.parametrize(
    "gap_nm, verdict", [(150_000, "match"), (250_000, "deviation")]
)
def test_segmented_terminal_routes_keep_trunk_checks_within_250ms(
    gap_nm: int, verdict: str, record_property: Callable[[str, object], None]
) -> None:
    """Keep both landing/trunk cases and all three cold contexts per worker."""
    assert (gap_nm, verdict) in ((150_000, "match"), (250_000, "deviation"))
    _isolated_spacing_samples("terminal-" + verdict, record_property)


def _terminal_spacing_samples(gap_nm: int, verdict: str) -> list[float]:
    """Exclude connected fanouts without concealing wrong-spacing paired trunks."""
    snapshot, config = differential_terminal_draft(SimpleNamespace(model=model), gap_nm)
    analysis = analyze(config, snapshot)
    samples = []
    for _ in range(REPEATED_SAMPLES):
        context = spacing_checks.SpacingContext(snapshot)
        started = perf_counter()
        checks = spacing_checks.spacing_checks_for_sections(
            config, snapshot, analysis.sections, context=context
        )
        samples.append(perf_counter() - started)
        assert checks
        assert all(check.verdict == verdict for check in checks.values())
        assert all(check.coverage == "complete" for check in checks.values())
        assert all(check.transition_length_nm > 0 for check in checks.values())
        if verdict == "deviation":
            assert all(
                check.issues
                and all(issue.location is not None for issue in check.issues)
                for check in checks.values()
            )
    return samples


def test_export_preflight_derives_spacing_once_and_currentness_does_not_repeat_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deriving required close-ups is bounded; repeated currentness reuses the plan."""
    snapshot, draft = differential_routed_draft(SimpleNamespace(model=model))
    analysis = analyze(draft, snapshot)
    config = replace(
        draft,
        reviewed_digest=analysis.digest,
        included_section_ids=tuple(section.section_id for section in analysis.sections),
    )

    calls = []
    original = spacing_checks.measure_pair_spacing

    def measured(*args: Any, **kwargs: Any) -> Any:
        calls.append((args[1], args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(spacing_checks, "measure_pair_spacing", measured)
    plan = service.prepare(config, snapshot)
    assert plan is not None
    assert len(calls) == len(snapshot.differential_pairs)
    before = tuple(calls)
    for _ in range(REPEATED_SAMPLES):
        service.validate_current(plan, config, snapshot)
    assert tuple(calls) == before


def test_nonparallel_long_pair_respects_work_limit_without_reporting_match(
    record_property: Callable[[str, object], None],
) -> None:
    """Retain the one-sample work-limit budget and uncertainty assertions."""
    _isolated_spacing_samples("work-limit", record_property)


def _work_limit_sample() -> list[float]:
    """Limited refinement must expose unchecked routing rather than pass it."""
    snapshot, _config = differential_routed_draft(SimpleNamespace(model=model))
    pair = snapshot.differential_pairs[0]
    snapshot = replace(
        snapshot,
        traces=tuple(
            replace(trace, points=tuple((x, y + x // 100) for x, y in trace.points))
            if trace.net == pair[1]
            else trace
            for trace in snapshot.traces
        ),
    )
    started = perf_counter()
    measurement = spacing_checks.measure_pair_spacing(
        snapshot,
        pair,
        "F.Cu",
        limits=spacing_checks.SpacingLimits(max_evaluations=8, max_intervals=4),
    )
    seconds = perf_counter() - started
    check = spacing_checks.compare_pair_spacing(measurement, 150_000)
    assert check.verdict != "match"
    assert check.coverage != "complete"
    assert check.unverified_length_nm > 0
    assert any("work limit" in reason.lower() for reason in check.reasons)
    return [seconds]


def _spacing_worker(case: str) -> None:
    """Run one allowlisted original case; emit all samples without retrying."""
    if not __debug__:
        raise RuntimeError("Spacing worker requires enabled functional assertions")
    assert case in _WORKER_CASES, case
    before = {"enabled": gc.isenabled(), "threshold": gc.get_threshold()}
    if case == "routed":
        samples = _routed_spacing_samples()
    elif case == "terminal-match":
        samples = _terminal_spacing_samples(150_000, "match")
    elif case == "terminal-deviation":
        samples = _terminal_spacing_samples(250_000, "deviation")
    else:
        samples = _work_limit_sample()
    after = {"enabled": gc.isenabled(), "threshold": gc.get_threshold()}
    sys.stdout.write(
        json.dumps(
            {"case": case, "samples": samples, "gc_before": before, "gc_after": after}
        )
        + "\n"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--spacing-worker":
        raise SystemExit("Expected --spacing-worker and one known benchmark case")
    _spacing_worker(sys.argv[2])
