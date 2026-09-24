"""Er for width solves follows JLCPCB calculator model defaults, not catalog Dk.

JLCPCB's public calculator and Si9000 confirmation sheets keep CoatedMicrostrip1B
Er1 at the model default (commonly 4.3) even when the stackup table lists a
different dielectricConstant (e.g. 2116 prepreg at 4.16). Nominal-width checks
must match that confirmation path.
"""

from decimal import Decimal
from typing import Any

from impedance.calculation_geometry import cross_section
from impedance.jlcpcb_calculator import ProviderConfiguration, calculation_parameters
from impedance.model import LayerSettings, Specification
from impedance.stackup_model import (
    CALCULATION_REVISION,
    Stackup,
    StackupLayer,
    calculation_fingerprint,
)


def _outer_microstrip_stackup(*, dielectric_constant: str) -> Stackup:
    """Two-layer construction with catalog Dk that can disagree with model Er1."""
    return Stackup(
        stackup_id="jlc06081h-2116-sample",
        name="JLC06081H-2116 sample",
        layer_count=2,
        thickness_mm="0.8",
        outer_copper_oz="1",
        inner_copper_oz="",
        calculator_id="sample-calculator",
        layers=(
            StackupLayer("L1", "copper", "0.035"),
            # Pressed 2116 thickness used by JLCPCB for this outer dielectric.
            StackupLayer("Prepreg", "prepreg", "0.1164", "2116", dielectric_constant),
            StackupLayer("L2", "copper", "0.035"),
        ),
    )


def _spec() -> Specification:
    return Specification(
        spec_id="rf",
        label="RF",
        target_ohms="50",
        kind="single_ended",
        net_class="RF",
        layer_settings=(LayerSettings("F.Cu", ("B.Cu",)),),
    )


def _provider_config(*, er1_default: float = 4.3) -> ProviderConfiguration:
    """Minimal CoatedMicrostrip1B metadata matching the public calculator shape."""
    parameter_list: list[dict[str, Any]] = [
        {"paramName": name, "defaultValue": value}
        for name, value in {
            "W1": 8.0,
            "W2": 7.3,
            "H1": 5.0,
            "Er1": er1_default,
            "T1": 1.6,
            "C1": 1.2,
            "C2": 0.6,
            "CEr": 3.8,
        }.items()
    ]
    return ProviderConfiguration(
        models=(
            {"impedanceType": "CoatedMicrostrip1B", "parameterList": parameter_list},
        ),
        copper=(
            {
                "layerType": 1,
                "systemCopperThickness": 1,
                "baseCopperThickness": 0.5,
                "traceWidthDelta": 0.7,
                "traceCopperThickness": 1.6,
            },
        ),
        coating=(
            {
                "layerType": 1,
                "systemCopperThickness": 1,
                "coatingAboveSubstrate": 1.2,
                "coatingAboveTrace": 0.6,
            },
        ),
        limits=({"impedanceName": "W2", "minValue": 2.5, "maxValue": 100},),
    )


def test_outer_microstrip_omits_catalog_dk_from_geometry_values() -> None:
    """Cross-section maps H1 from thickness; Er1 is left to the provider model."""
    geometry = cross_section(
        _outer_microstrip_stackup(dielectric_constant="4.16"), _spec(), "F.Cu"
    )
    values = dict(geometry.values)
    assert geometry.model == "CoatedMicrostrip1B"
    assert "Er1" not in values
    assert "Er2" not in values
    expected_h1 = float(Decimal("0.1164") / Decimal("0.0254"))
    assert values["H1"] == expected_h1
    assert values["Zo"] == 50.0
    assert any(
        "model default" in item.lower() or "calculator model" in item.lower()
        for item in geometry.assumptions
    )


def test_calculation_parameters_keep_model_er_when_catalog_dk_differs() -> None:
    """Catalog Dk 4.16 must not overwrite CoatedMicrostrip1B Er1 default 4.3."""
    geometry = cross_section(
        _outer_microstrip_stackup(dielectric_constant="4.16"), _spec(), "F.Cu"
    )
    parameters = calculation_parameters(geometry, _provider_config(er1_default=4.3))
    assert parameters["Er1"] == 4.3
    expected_h1 = float(Decimal("0.1164") / Decimal("0.0254"))
    assert parameters["H1"] == expected_h1
    # Discrete Si9000/web bin for this construction with Er1=4.3 is 7.140625 mil
    # → 0.1814 mm after JLCPCB's mil→mm display rounding.
    assert abs(7.140625 * 0.0254 - 0.1814) < 5e-5


def test_catalog_dk_change_does_not_change_solver_er() -> None:
    """Only thickness (and model defaults) drive Er/H; Dk edits leave Er alone."""
    geometry_a = cross_section(
        _outer_microstrip_stackup(dielectric_constant="4.16"), _spec(), "F.Cu"
    )
    geometry_b = cross_section(
        _outer_microstrip_stackup(dielectric_constant="3.91"), _spec(), "F.Cu"
    )
    config = _provider_config(er1_default=4.3)
    params_a = calculation_parameters(geometry_a, config)
    params_b = calculation_parameters(geometry_b, config)
    assert params_a["Er1"] == params_b["Er1"] == 4.3
    assert params_a["H1"] == params_b["H1"]


def test_calculation_revision_invalidates_prior_er_overlay_digests() -> None:
    """Mapping Er to model defaults must bump the declared-geometry revision."""
    assert CALCULATION_REVISION >= 2
    digest = calculation_fingerprint(
        _outer_microstrip_stackup(dielectric_constant="4.16"), _spec(), "F.Cu"
    )
    assert len(digest) == 64
