"""Regression coverage for offline nominal-width comparison messages."""

from __future__ import annotations

from dataclasses import replace
import importlib
from types import SimpleNamespace

from .test_impedance_draft_dialog import (
    _intent,
    constructor_api,  # noqa: F401 -- fixture dependency
    draft_api as _draft_api,
)

draft_api = _draft_api


def _stackup(api: SimpleNamespace):
    """Build a calculator-supported two-layer construction for message checks."""
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    return model.Stackup(
        "sample",
        "Sample two-layer construction",
        2,
        "0.324",
        "1",
        "",
        calculator_id="sample-calculator",
        layers=(
            model.StackupLayer("L1", "copper", "0.035"),
            model.StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
            model.StackupLayer("L2", "copper", "0.035"),
        ),
    )


def test_width_comparison_rows_use_three_aligned_columns() -> None:
    """Labels stay right-aligned while mm and mil values each keep a left column."""
    from impedance.width_checks import width_comparison_rows

    rows = width_comparison_rows(150_000, 175_000, -25_000)
    assert [(row.label, row.millimetres, row.mils) for row in rows] == [
        ("Actual:", "0.15 mm", "(5.91 mil)"),
        ("Nominal:", "0.175 mm", "(6.89 mil)"),
        ("Difference:", "-0.025 mm", "(-0.98 mil)"),
    ]
    assert all(row.label.endswith(":") or row.label == "" for row in rows)


def test_select_comparison_width_prefers_nominal_match_then_frequency() -> None:
    """One SpecDialog comparison uses the matching or dominant actual width."""
    from impedance.width_checks import select_comparison_width_nm

    assert select_comparison_width_nm([100_000, 150_000, 100_000], 150_000) == 150_000
    assert select_comparison_width_nm([100_000, 150_000, 100_000], 175_000) == 100_000
    assert select_comparison_width_nm([150_000], None) == 150_000


def test_width_check_messages_explain_readiness(draft_api: SimpleNamespace) -> None:
    """Missing results and incomplete inputs must not share a vague stackup/layer string."""
    api = draft_api
    checks = importlib.import_module(api.dialog.__package__ + ".width_checks")
    model = importlib.import_module(api.dialog.__package__ + ".stackup_model")
    stackup = _stackup(api)
    spec = _intent(api).specifications[0]
    actual = 150_000

    no_stackup = checks.width_check(
        api.model.Config(specifications=(spec,)), spec, "F.Cu", actual
    )
    assert "select a JLCPCB stackup" in no_stackup.message

    pending = checks.width_check(
        api.model.Config(specifications=(spec,), stackup=stackup),
        spec,
        "F.Cu",
        actual,
    )
    assert "waiting for JLCPCB's calculator" in pending.message
    assert "selected stackup and layer" not in pending.message

    incomplete = checks.width_check(
        api.model.Config(
            specifications=(spec,),
            stackup=replace(stackup, calculator_id=""),
        ),
        spec,
        "F.Cu",
        actual,
    )
    assert "no JLCPCB calculator construction" in incomplete.message

    digest = model.calculation_fingerprint(stackup, spec, "F.Cu")
    historical = checks.width_check(
        api.model.Config(
            specifications=(spec,),
            stackup=stackup,
            width_results=(
                model.WidthResult(
                    spec_id=spec.spec_id,
                    layer="F.Cu",
                    input_digest="0" * 64,
                    status="success",
                    target_width_nm=actual,
                    calculated_at_utc="2026-09-23T00:00:00.000000Z",
                    model="CoatedMicrostrip1B",
                    calculation_digest="a" * 64,
                ),
            ),
        ),
        spec,
        "F.Cu",
        actual,
    )
    assert "inputs changed" in historical.message

    match = checks.width_check(
        api.model.Config(
            specifications=(spec,),
            stackup=stackup,
            width_results=(
                model.WidthResult(
                    spec_id=spec.spec_id,
                    layer="F.Cu",
                    input_digest=digest,
                    status="success",
                    target_width_nm=actual,
                    calculated_at_utc="2026-09-23T00:00:00.000000Z",
                    model="CoatedMicrostrip1B",
                    calculation_digest="a" * 64,
                ),
            ),
        ),
        spec,
        "F.Cu",
        actual,
    )
    assert match.status == "matches_nominal"
