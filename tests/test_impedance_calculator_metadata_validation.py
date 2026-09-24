"""Reject poisoned provider metadata without turning failures into settled results."""

from copy import deepcopy
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from impedance import jlcpcb_calculator as calculator, stackup_model
from impedance.calculator_config_cache import (
    CalculatorConfigCache,
    calculator_config_checked_at,
    calculator_config_from_payload,
    calculator_config_to_payload,
)

from .test_jlcpcb_calculator_transport import _inputs, _Provider, provider as _provider

provider = _provider


def _configuration() -> calculator.ProviderConfiguration:
    """Use independent wire metadata from the existing sample provider."""
    source = _Provider()
    rows = []
    for endpoint, container in (
        ("selectPageImpedancePicture", "body"),
        ("impedance-config/copper-trace-width/list", "data"),
        ("impedance-config/coverlay/list", "data"),
        ("selectImpedanceDefaultValue", "data"),
    ):
        rows.append(
            tuple(
                source.post(calculator.API_PREFIX + endpoint, {}, timeout=1)[container]
            )
        )
    return calculator.ProviderConfiguration(*rows)


def _poison(
    config: calculator.ProviderConfiguration, problem: str
) -> calculator.ProviderConfiguration:
    """Introduce one semantic defect while preserving the cached JSON structure."""
    config = deepcopy(config)
    parameters = config.models[0]["parameterList"]
    if problem == "invalid-default":
        parameters[0]["defaultValue"] = "not a number"
    elif problem == "unknown-parameter":
        parameters.append({"paramName": "FutureParameter", "defaultValue": 1})
    elif problem == "duplicate-parameter":
        parameters.append(dict(parameters[0]))
    elif problem == "missing-width":
        parameters[:] = [row for row in parameters if row["paramName"] != "W1"]
    elif problem == "missing-dielectric":
        parameters[:] = [row for row in parameters if row["paramName"] != "Er1"]
    elif problem == "missing-coating-parameter":
        parameters[:] = [row for row in parameters if row["paramName"] != "C2"]
    elif problem == "duplicate-model":
        config = replace(config, models=config.models + deepcopy(config.models))
    elif problem == "missing-model":
        config.models[0]["impedanceType"] = "DiffEdgeCoupledCoatedMicrostrip1B"
    elif problem == "invalid-copper":
        config.copper[0]["traceCopperThickness"] = "NaN"
    elif problem == "invalid-selector":
        config.copper[0]["layerType"] = True
    elif problem == "missing-copper":
        config.copper[0]["systemCopperThickness"] = 2
    elif problem == "missing-copper-layer":
        config.copper[0]["layerType"] = 2
    elif problem == "missing-coating":
        config.coating[0]["systemCopperThickness"] = 2
    elif problem == "missing-coating-layer":
        config.coating[0]["layerType"] = 2
    elif problem == "ambiguous-copper":
        config = replace(config, copper=config.copper + deepcopy(config.copper))
    elif problem == "invalid-coating":
        config.coating[0]["coatingAboveTrace"] = -1
    elif problem == "invalid-limits":
        config.limits[0]["minValue"] = config.limits[0]["maxValue"]
    else:
        raise AssertionError(problem)
    return config


MISSING_MANUFACTURING = (
    "missing-copper",
    "missing-copper-layer",
    "missing-coating",
    "missing-coating-layer",
)

PROBLEMS = (
    "invalid-default",
    "unknown-parameter",
    "duplicate-parameter",
    "missing-width",
    "missing-dielectric",
    "missing-coating-parameter",
    "duplicate-model",
    "missing-model",
    "invalid-copper",
    "invalid-selector",
    "ambiguous-copper",
    "invalid-coating",
    "invalid-limits",
)


@pytest.mark.parametrize("problem", PROBLEMS + MISSING_MANUFACTURING)
def test_fresh_poisoned_cache_refetches_once_and_recovers(
    provider: _Provider, problem: str
) -> None:
    """A seven-day-fresh cache cannot prevent a corrected provider from recovering."""
    bad = _poison(_configuration(), problem)
    stored = CalculatorConfigCache(
        bad.models,
        bad.copper,
        bad.coating,
        bad.limits,
        calculator_config_checked_at(),
    )
    restored = calculator_config_from_payload(calculator_config_to_payload(stored))
    requests = []
    saved = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        requests.append(path.removeprefix(calculator.API_PREFIX))
        return provider.post(path, payload, **kwargs)

    client = calculator.JlcpcbCalculator(
        post=post,
        configuration=calculator.ProviderConfiguration(
            restored.models, restored.copper, restored.coating, restored.limits
        ),
        save_configuration=saved.append,
    )
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "success"
    assert len(requests) == 5  # four metadata envelopes and one correlated solve
    assert len(saved) == 1
    assert client._configuration == saved[0]


@pytest.mark.parametrize("problem", PROBLEMS)
def test_invalid_fetched_metadata_is_not_published_and_next_attempt_recovers(
    provider: _Provider, problem: str
) -> None:
    """Incomplete provider data stays retryable and cannot be marked fresh in memory/DB."""
    bad = _poison(_configuration(), problem)
    broken = [True]
    requests = []
    saved = []
    envelopes = {
        "selectPageImpedancePicture": ("body", bad.models),
        "impedance-config/copper-trace-width/list": ("data", bad.copper),
        "impedance-config/coverlay/list": ("data", bad.coating),
        "selectImpedanceDefaultValue": ("data", bad.limits),
    }

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        endpoint = path.removeprefix(calculator.API_PREFIX)
        requests.append(endpoint)
        if broken[0] and endpoint in envelopes:
            container, records = envelopes[endpoint]
            return {"code": 200, container: list(records)}
        return provider.post(path, payload, **kwargs)

    client = calculator.JlcpcbCalculator(post=post, save_configuration=saved.append)
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "unavailable"
    assert result.target_width_nm is None
    assert client._configuration is None
    assert saved == []
    assert len(requests) == 4  # no unbounded refetch loop or solver submission
    broken[0] = False
    assert client.calculate_width(*_inputs(), "F.Cu").status == "success"
    assert len(saved) == 1
    assert len(requests) == 9


def test_valid_subset_and_additive_fields_remain_usable(provider: _Provider) -> None:
    """Known model subsets and provider additions do not require a schema lockstep."""
    config = _configuration()
    config.models[0]["newProviderField"] = {"anything": True}
    config.models[0]["parameterList"].append(
        {"paramName": "UnusedDisplayField", "displayStatus": 4}
    )
    unknown = {"impedanceType": "FutureModel", "futureData": True}
    config = replace(config, models=config.models + (unknown,))
    client = calculator.JlcpcbCalculator(post=provider.post, configuration=config)
    assert client.calculate_width(*_inputs(), "F.Cu").status == "success"


@pytest.mark.parametrize("base_weight", (0.5, "0.50", ".5"))
def test_unique_half_ounce_manufacturing_selection_is_preserved(
    provider: _Provider, base_weight: object
) -> None:
    """Existing provider duplicates have one deliberate 0.5 oz base-copper choice."""
    config = _configuration()
    preferred = {**config.copper[0], "baseCopperThickness": base_weight}
    other = {**config.copper[0], "baseCopperThickness": 1, "traceWidthDelta": 1}
    config = replace(config, copper=(other, preferred))
    requests = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        requests.append(path.removeprefix(calculator.API_PREFIX))
        return provider.post(path, payload, **kwargs)

    client = calculator.JlcpcbCalculator(post=post, configuration=config)
    assert client.calculate_width(*_inputs(), "F.Cu").status == "success"
    assert provider.submissions[0]["impedance_calc_arg"]["W2LinkW1Incr"] == 0.5
    assert requests == ["calc"]


def test_cache_persistence_failure_retains_valid_loaded_configuration(
    provider: _Provider,
) -> None:
    """Optional storage failure must not trigger repeated metadata fetches."""
    requests = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        requests.append(path)
        return provider.post(path, payload, **kwargs)

    def save(_configuration: calculator.ProviderConfiguration) -> None:
        raise OSError("Locked optional cache")

    client = calculator.JlcpcbCalculator(post=post, save_configuration=save)
    assert client.calculate_width(*_inputs(), "F.Cu").status == "success"
    loaded = client._configuration
    assert loaded is not None
    assert client._load_configuration(time.monotonic() + 1, None) is loaded
    assert len(requests) == 5


@pytest.mark.parametrize("stop", ("cancel", "deadline"))
def test_recovery_fetch_obeys_original_budget_and_never_publishes_incomplete_data(
    monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """Discarding an invalid cache does not grant a new deadline or ignore cancellation."""
    metadata = _configuration()
    records = (metadata.models, metadata.copper, metadata.coating, metadata.limits)
    now = [0.0]
    requests = []
    saved = []
    cancel = threading.Event()
    monkeypatch.setattr(calculator, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def post(_path: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        index = len(requests)
        requests.append(index)
        if stop == "deadline":
            now[0] += 30
        elif index == 3:
            cancel.set()
        return {"code": 200, "body" if index == 0 else "data": list(records[index])}

    client = calculator.JlcpcbCalculator(
        post=post,
        connector=lambda **_kwargs: None,
        configuration=_poison(metadata, "invalid-default"),
        save_configuration=saved.append,
    )
    if stop == "cancel":
        with pytest.raises(calculator.CalculatorCancelled):
            client.calculate_width(*_inputs(), "F.Cu", cancel=cancel)
        assert len(requests) == 4
    else:
        result = client.calculate_width(*_inputs(), "F.Cu", cancel=cancel)
        assert result.status == "unavailable"
        assert "timed out" in result.message
        assert len(requests) == 2
    assert client._configuration is None
    assert saved == []


def test_invalid_cache_and_invalid_provider_make_only_one_complete_fetch() -> None:
    """A poisoned cache may trigger one fetch, never an immediate recovery loop."""
    bad = _poison(_configuration(), "invalid-default")
    records = (bad.models, bad.copper, bad.coating, bad.limits)
    requests = []
    saved = []

    def post(_path: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        index = len(requests)
        requests.append(index)
        return {"code": 200, "body" if index == 0 else "data": list(records[index])}

    client = calculator.JlcpcbCalculator(
        post=post,
        connector=lambda **_kwargs: None,
        configuration=bad,
        save_configuration=saved.append,
    )
    assert client.calculate_width(*_inputs(), "F.Cu").status == "unavailable"
    assert len(requests) == 4
    assert client._configuration is None
    assert saved == []


def test_calculation_revision_retires_prevalidation_settled_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old cached unsupported verdicts cannot bypass the corrected metadata checks."""
    stackup, spec = _inputs()
    assert stackup_model.CALCULATION_REVISION >= 3
    current = stackup_model.calculation_fingerprint(stackup, spec, "F.Cu")
    monkeypatch.setattr(stackup_model, "CALCULATION_REVISION", 2)
    assert current != stackup_model.calculation_fingerprint(stackup, spec, "F.Cu")
