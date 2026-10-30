"""An authoritative missing construction settles without discarding usable metadata."""

from copy import deepcopy
from dataclasses import replace
import threading
from typing import Any

import pytest

from impedance import jlcpcb_calculator as calculator
from impedance._vendor.websockets.protocol import CLOSED, State
from impedance._vendor.websockets.sync.server import ServerConnection
from impedance.calculator_config_cache import (
    CalculatorConfigCache,
    calculator_config_checked_at,
    calculator_config_from_payload,
    calculator_config_to_payload,
)

from .test_impedance_calculator_metadata_validation import _configuration
from .test_jlcpcb_calculator_transport import _inputs, _Provider, provider as _provider

provider = _provider


def _envelopes(
    configuration: calculator.ProviderConfiguration,
) -> dict[str, tuple[str, tuple[dict[str, Any], ...]]]:
    """Expose a complete independent snapshot through the four public envelopes."""
    return {
        "selectPageImpedancePicture": ("body", configuration.models),
        "impedance-config/copper-trace-width/list": ("data", configuration.copper),
        "impedance-config/coverlay/list": ("data", configuration.coating),
        "selectImpedanceDefaultValue": ("data", configuration.limits),
    }


def _persisted(
    configuration: calculator.ProviderConfiguration,
) -> calculator.ProviderConfiguration:
    """Exercise the stored payload rather than passing the same mutable rows back."""
    cache = CalculatorConfigCache(
        configuration.models,
        configuration.copper,
        configuration.coating,
        configuration.limits,
        calculator_config_checked_at(),
    )
    restored = calculator_config_from_payload(calculator_config_to_payload(cache))
    return calculator.ProviderConfiguration(
        restored.models, restored.copper, restored.coating, restored.limits
    )


@pytest.mark.parametrize("table", ("copper", "coating"))
@pytest.mark.parametrize("selector", ("weight", "layer"))
@pytest.mark.parametrize("cached", (False, True))
def test_missing_authoritative_manufacturing_support_settles_and_keeps_metadata(
    table: str, selector: str, cached: bool
) -> None:
    """A valid table without the exact construction is unsupported, not a retry."""
    configuration = _configuration()
    rows = getattr(configuration, table)
    rows[0]["systemCopperThickness" if selector == "weight" else "layerType"] = 2
    envelopes = _envelopes(configuration)
    requests: list[str] = []
    saved: list[calculator.ProviderConfiguration] = []

    def post(path: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Fail immediately if an unsupported construction reaches the solver."""
        endpoint = path.removeprefix(calculator.API_PREFIX)
        requests.append(endpoint)
        assert endpoint in envelopes, "Unsupported geometry must not be submitted"
        container, records = envelopes[endpoint]
        return {"code": 200, container: deepcopy(list(records))}

    client = calculator.JlcpcbCalculator(
        post=post,
        configuration=_persisted(configuration) if cached else None,
        save_configuration=saved.append,
    )
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "unsupported", result.message
    assert result.target_width_nm is None and result.calculation_digest == ""
    assert "1 oz" in result.message and "outer" in result.message
    assert ("copper" if table == "copper" else "soldermask") in result.message
    assert len(requests) == 4
    assert saved == [configuration]
    assert client._configuration == configuration

    # Another job with the same unsupported construction cannot refetch a
    # snapshot that this client just obtained directly from the provider.
    assert client.calculate_width(*_inputs(), "F.Cu").status == "unsupported"
    assert len(requests) == 4 and len(saved) == 1


@pytest.mark.parametrize("table", ("copper", "coating"))
@pytest.mark.parametrize("cached", (False, True))
@pytest.mark.parametrize("supported_first", (False, True))
def test_unsupported_weight_keeps_supported_neighbor_usable_after_reopening(
    provider: _Provider, table: str, cached: bool, supported_first: bool
) -> None:
    """One unsupported job must not refetch or poison supported jobs in its batch."""
    configuration = _configuration()
    other_table = "coating" if table == "copper" else "copper"
    other_rows = getattr(configuration, other_table)
    configuration = replace(
        configuration,
        **{other_table: other_rows + ({**other_rows[0], "systemCopperThickness": 2},)},
    )
    envelopes = _envelopes(configuration)
    requests: list[str] = []
    saved: list[calculator.ProviderConfiguration] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Use the real subscription and submission for the supported neighbor."""
        endpoint = path.removeprefix(calculator.API_PREFIX)
        requests.append(endpoint)
        if endpoint in envelopes:
            container, records = envelopes[endpoint]
            return {"code": 200, container: deepcopy(list(records))}
        return provider.post(path, payload, **kwargs)

    client = calculator.JlcpcbCalculator(
        post=post,
        configuration=_persisted(configuration) if cached else None,
        save_configuration=saved.append,
    )
    stackup, spec = _inputs()
    unsupported_stackup = replace(stackup, outer_copper_oz="2")
    if supported_first:
        result = client.calculate_width(stackup, spec, "F.Cu")
        assert result.status == "success", result.message
        assert provider.closed.wait(1), "The first subscription did not close"
        provider.connected.clear()
        provider.closed.clear()
    result = client.calculate_width(unsupported_stackup, spec, "F.Cu")
    assert result.status == "unsupported", result.message
    assert len(requests) == 4 + supported_first
    assert len(provider.submissions) == supported_first
    assert len(saved) == 1

    result = client.calculate_width(stackup, spec, "F.Cu")
    assert result.status == "success", result.message
    assert requests[-1] == "calc" and len(requests) == 5 + supported_first
    assert provider.closed.wait(1), "The neighboring subscription did not close"
    provider.connected.clear()
    provider.closed.clear()

    # A later worker can reuse the persisted snapshot for supported inputs.
    reopened = calculator.JlcpcbCalculator(
        post=post, configuration=_persisted(saved[0]), save_configuration=saved.append
    )
    result = reopened.calculate_width(stackup, spec, "F.Cu")
    assert result.status == "success", result.message
    assert requests[-1] == "calc" and len(requests) == 6 + supported_first
    assert len(saved) == 1 and len(provider.submissions) == 2 + supported_first


def test_reopening_waits_for_the_new_subscription_handler(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A completed handshake cannot reuse the previous subscription's readiness."""
    original_handle = _Provider.handle
    second_handler = threading.Event()
    release_handler = threading.Event()
    connections: list[ServerConnection] = []

    def delayed_handle(provider: _Provider, connection: ServerConnection) -> None:
        """Delay the second handler until the new HTTP submission enters."""
        connections.append(connection)
        if len(connections) == 2:
            second_handler.set()
            assert release_handler.wait(2), "The second handler was never released"
        original_handle(provider, connection)

    # The shared fixture captures this bound method when starting its server.
    monkeypatch.setattr(_Provider, "handle", delayed_handle)
    provider = request.getfixturevalue("provider")
    original_post = provider.post
    readiness: list[tuple[bool, State]] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Observe readiness before allowing the server to replace the old socket."""
        if path.endswith("/calc") and len(provider.submissions) == 1:
            assert second_handler.wait(1), "The reopened subscription never arrived"
            readiness.append((provider.connected.is_set(), provider.connection.state))
            # Release at HTTP entry, before post waits for current readiness.
            release_handler.set()
        return original_post(path, payload, **kwargs)

    monkeypatch.setattr(provider, "post", post)
    failures: list[AssertionError] = []
    try:
        test_unsupported_weight_keeps_supported_neighbor_usable_after_reopening(
            provider, "copper", False, False
        )
    except AssertionError as error:
        failures.append(error)
    finally:
        release_handler.set()
    assert readiness == [(False, CLOSED)], (
        "The new HTTP submission reused readiness from the previous closed socket"
    )
    if failures:
        raise failures[0]
