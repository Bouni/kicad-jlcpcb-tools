"""A valid unrelated model must not block nominal widths for RF and USB."""

from dataclasses import replace
from typing import Any

import pytest

from impedance import jlcpcb_calculator as calculator
from impedance.model import LayerSettings

from .test_impedance_calculator_metadata_validation import _configuration
from .test_jlcpcb_calculator_transport import _inputs, _Provider, provider as _provider

provider = _provider


def _catalog_configuration() -> calculator.ProviderConfiguration:
    """Keep active defaults and inactive C3 from public metadata on 2026-10-22.

    Display-only fields are omitted; active display statuses are normalized to 1.
    The single-ended coplanar model deliberately comes first, as in that response.
    Manufacturing rows remain the independent loopback provider's sample values.
    """
    defaults = (
        (
            "CoatedCoplanarWaveguideWithLowerGnd1B",
            {
                "H1": 4.5,
                "Er1": 4.3,
                "W1": 7,
                "W2": 6.3,
                "D1": 20,
                "T1": 1.6,
                "C1": 1.2,
                "C2": 0.6,
                "C3": None,
                "CEr": 3.8,
            },
        ),
        (
            "OffsetStripline1B1A",
            {
                "H1": 4.25,
                "Er1": 4.3,
                "H2": 40,
                "Er2": 4.2,
                "W1": 7,
                "W2": 6.5,
                "T1": 0.6,
                "C3": None,
            },
        ),
        (
            "OffsetCoplanarWaveguide1B1A",
            {
                "H1": 4.25,
                "Er1": 4.3,
                "H2": 4.25,
                "Er2": 4.3,
                "W1": 7,
                "W2": 6.5,
                "D1": 8,
                "T1": 0.6,
                "C3": None,
            },
        ),
        (
            "DiffCoatedCoplanarWaveguideWithLowerGnd1B",
            {
                "H1": 8.5,
                "Er1": 4.3,
                "W1": 7,
                "W2": 6.3,
                "S1": 8,
                "D1": 8,
                "T1": 1.6,
                "C1": 1.2,
                "C2": 0.6,
                "C3": 1.2,
                "CEr": 3.8,
            },
        ),
        (
            "DiffOffsetCoplanarWaveguide1B1A",
            {
                "H1": 4.25,
                "Er1": 4.3,
                "H2": 4.25,
                "Er2": 4.3,
                "W1": 7,
                "W2": 6.5,
                "S1": 8,
                "D1": 8,
                "T1": 0.6,
                "C3": None,
            },
        ),
        (
            "DiffOffsetStripline1B1A",
            {
                "H1": 4.25,
                "Er1": 4.3,
                "H2": 4.25,
                "Er2": 4.3,
                "W1": 7,
                "W2": 6.5,
                "S1": 8,
                "T1": 0.6,
                "C3": None,
            },
        ),
        (
            "CoatedMicrostrip1B",
            {
                "H1": 5,
                "Er1": 4.3,
                "W1": 8,
                "W2": 7.3,
                "T1": 1.6,
                "C1": 1.2,
                "C2": 0.6,
                "C3": None,
                "CEr": 3.8,
            },
        ),
        (
            "DiffEdgeCoupledCoatedMicrostrip1B",
            {
                "H1": 5,
                "Er1": 4.3,
                "W1": 5.2,
                "W2": 4.5,
                "S1": 8,
                "T1": 1.6,
                "C1": 1.2,
                "C2": 0.6,
                "C3": 1.2,
                "CEr": 3.8,
            },
        ),
    )
    config = _configuration()
    return replace(
        config,
        models=tuple(
            {
                "impedanceType": model,
                "parameterList": [
                    {
                        "paramName": name,
                        "defaultValue": value,
                        "displayStatus": 4 if value is None else 1,
                    }
                    for name, value in values.items()
                ],
            }
            for model, values in defaults
        ),
        coating=({**config.coating[0], "coatingBetweenTraces": 0.7},),
    )


@pytest.mark.parametrize("cached", [False, True], ids=["fetched", "cached"])
@pytest.mark.parametrize(
    ("kind", "target", "expected_model", "has_c3"),
    [
        ("single_ended", "50", "CoatedMicrostrip1B", False),
        ("single_ended_coplanar", "50", "CoatedCoplanarWaveguideWithLowerGnd1B", False),
        ("differential", "90", "DiffEdgeCoupledCoatedMicrostrip1B", True),
        (
            "differential_coplanar",
            "90",
            "DiffCoatedCoplanarWaveguideWithLowerGnd1B",
            True,
        ),
    ],
)
def test_full_catalog_allows_outer_widths(
    provider: _Provider,
    cached: bool,
    kind: str,
    target: str,
    expected_model: str,
    has_c3: bool,
) -> None:
    """All outer models solve despite an unrelated model's inactive null C3."""
    config = _catalog_configuration()
    requests: list[str] = []
    saved: list[calculator.ProviderConfiguration] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        endpoint = path.removeprefix(calculator.API_PREFIX)
        requests.append(endpoint)
        if endpoint == "selectPageImpedancePicture":
            return {"result": "success", "body": list(config.models)}
        if endpoint == "impedance-config/coverlay/list":
            return {"code": 200, "data": list(config.coating)}
        return provider.post(path, payload, **kwargs)

    stackup, spec = _inputs()
    spec = replace(
        spec,
        kind=kind,
        target_ohms=target,
        layer_settings=(
            LayerSettings("F.Cu", ("B.Cu",), spacing_nm=254_000, ground_gap_nm=254_000),
        ),
    )
    client = calculator.JlcpcbCalculator(
        post=post,
        configuration=config if cached else None,
        save_configuration=saved.append,
    )
    result = client.calculate_width(stackup, spec, "F.Cu")

    assert result.status == "success", result.message
    assert result.model == expected_model
    assert result.target_width_nm == 215_900
    assert requests[-1] == "calc"
    assert len(requests) == (1 if cached else 5)
    assert len(saved) == (0 if cached else 1)
    parameters = provider.submissions[0]["impedance_calc_arg"]
    assert parameters["Zo"] == float(target)
    if has_c3:
        assert parameters["C3"] == 0.7  # Manufacturing coating, not model default 1.2.
    else:
        assert "C3" not in parameters


@pytest.mark.parametrize(
    "model",
    ["DiffEdgeCoupledCoatedMicrostrip1B", "DiffCoatedCoplanarWaveguideWithLowerGnd1B"],
)
@pytest.mark.parametrize("problem", ["missing", "inactive"])
def test_differential_coating_is_required_before_catalog_adoption(
    provider: _Provider,
    model: str,
    problem: str,
) -> None:
    """Missing coating between differential traces stays retryable, never solved."""
    config = _catalog_configuration()
    record = next(item for item in config.models if item["impedanceType"] == model)
    if problem == "missing":
        record["parameterList"] = [
            item for item in record["parameterList"] if item["paramName"] != "C3"
        ]
    else:
        parameter = next(
            item for item in record["parameterList"] if item["paramName"] == "C3"
        )
        parameter.update(defaultValue=None, displayStatus=4)
    # Isolate the affected model so another model cannot mask the missing check.
    config = replace(config, models=(record,))
    saved: list[calculator.ProviderConfiguration] = []
    requests: list[str] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        endpoint = path.removeprefix(calculator.API_PREFIX)
        requests.append(endpoint)
        if endpoint == "selectPageImpedancePicture":
            return {"result": "success", "body": list(config.models)}
        if endpoint == "impedance-config/coverlay/list":
            return {"code": 200, "data": list(config.coating)}
        return provider.post(path, payload, **kwargs)

    stackup, spec = _inputs()
    spec = replace(
        spec,
        kind="differential_coplanar" if "Coplanar" in model else "differential",
        target_ohms="90",
        layer_settings=(
            LayerSettings("F.Cu", ("B.Cu",), spacing_nm=254_000, ground_gap_nm=254_000),
        ),
    )
    client = calculator.JlcpcbCalculator(post=post, save_configuration=saved.append)
    result = client.calculate_width(stackup, spec, "F.Cu")
    assert result.status == "unavailable"
    assert "missing required width or construction parameters" in result.message
    assert result.target_width_nm is None
    assert len(requests) == 4
    assert provider.submissions == []
    assert client._configuration is None
    assert saved == []
