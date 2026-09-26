"""Bounded worker-thread client for JLCPCB's public-facing calculator service.

The provider uses an undocumented frontend HTTP submission/WebSocket protocol.
No board content is uploaded: requests contain anonymous numerical cross-sections,
an allowlisted model identifier, and newly generated correlation identifiers only.
There is deliberately no local-formula fallback when the service is unavailable.
"""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
import logging
import ssl
import threading
import time
from typing import Any, Optional
import uuid

from .calculation_geometry import (
    CrossSection,
    UnsupportedCalculation,
    cross_section,
    positive_number,
)
from .model import Specification
from .review_tracking import utc_now
from .stackup_model import (
    Stackup,
    WidthResult,
    calculation_fingerprint,
    solver_fingerprint,
)

CALCULATOR_URL = "https://jlcpcb.com/pcb-impedance-calculator/"
WEBSOCKET_BASE = "wss://tools.jlc.com/jlcTools/webSocket/"
API_PREFIX = "/jlcTools/impedance/"
MAX_MESSAGE_BYTES = 128 * 1024
MAX_MESSAGES = 64
MAX_TOTAL_SECONDS = 60.0
PARAMETERS = frozenset(
    ("H1", "H2", "Er1", "Er2", "W1", "W2", "S1", "D1", "T1", "C1", "C2", "C3", "CEr")
)
MODELS = frozenset(
    (
        "CoatedMicrostrip1B",
        "DiffEdgeCoupledCoatedMicrostrip1B",
        "OffsetStripline1B1A",
        "DiffOffsetStripline1B1A",
        "CoatedCoplanarWaveguideWithLowerGnd1B",
        "DiffCoatedCoplanarWaveguideWithLowerGnd1B",
        "OffsetCoplanarWaveguide1B1A",
        "DiffOffsetCoplanarWaveguide1B1A",
    )
)

_LOG = logging.getLogger(__name__)


def _format_solve_parameters(parameters: dict[str, Any]) -> str:
    """Summarize solver inputs for the plugin log without dumping huge payloads."""
    parts = []
    for name in sorted(parameters):
        value = parameters[name]
        if isinstance(value, float):
            parts.append(f"{name}={value:g}")
        else:
            parts.append(f"{name}={value}")
    text = " ".join(parts)
    if len(text) > 800:
        return text[:797] + "..."
    return text


def _solver_rejection_message(response: dict[str, Any]) -> str:
    """Return a short English failure; never forward provider locale text."""
    return "JLCPCB could not calculate a width"


class CalculatorUnavailable(RuntimeError):
    """The provider cannot currently return a usable, correlated numerical result."""


class CalculatorCancelled(RuntimeError):
    """The owning dialog cancelled this calculation; no result should be applied."""


@dataclass(frozen=True)
class ProviderConfiguration:
    """A frozen, bounded model/manufacturing snapshot reused by one worker client."""

    models: tuple[dict[str, Any], ...]
    copper: tuple[dict[str, Any], ...]
    coating: tuple[dict[str, Any], ...]
    limits: tuple[dict[str, Any], ...]


def _json(value: object) -> str:
    """Produce reproducible finite JSON for provider requests and validation."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _provider_number(value: object, label: str) -> Decimal:
    """Distinguish malformed provider data from a genuinely unsupported user geometry."""
    try:
        return positive_number(value, label)
    except UnsupportedCalculation as exc:
        raise CalculatorUnavailable(f"JLCPCB returned an invalid {label}.") from exc


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    """Stop ownership-abandoned work without manufacturing a persisted failure result."""
    if cancel is not None and cancel.is_set():
        raise CalculatorCancelled("Impedance calculation cancelled.")


def _remaining(deadline: float, cancel: Optional[threading.Event]) -> float:
    """Return remaining total time, including metadata retrieval and socket setup."""
    _check_cancel(cancel)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CalculatorUnavailable("JLCPCB's calculator timed out.")
    return remaining


def _websocket_failure_message(exc: BaseException) -> str:
    """Describe a transport failure without instructing the user to retry manually."""
    name = type(exc).__name__.casefold()
    text = str(exc).casefold()
    if "ssl" in name or "certificate" in name or "ssl" in text:
        return "Could not establish a secure connection to JLCPCB's calculator."
    if "timeout" in name or "timed out" in text:
        return "JLCPCB's calculator connection timed out."
    if "closed" in name or "connection closed" in text:
        return "JLCPCB's calculator closed the connection before returning a result."
    if "refused" in text or "unreachable" in text or "name or service" in text:
        return "Could not connect to JLCPCB's calculator."
    return "Could not receive JLCPCB's calculator result."


def _default_post(
    path: str, payload: dict[str, object], **kwargs: Any
) -> dict[str, Any]:
    """Use the catalog's fixed-host, bounded public JSON transport."""
    from .jlcpcb_stackups import CatalogCancelled, CatalogError, post_json

    try:
        return post_json(path, payload, **kwargs)
    except CatalogCancelled as exc:
        raise CalculatorCancelled("Impedance calculation cancelled.") from exc
    except CatalogError as exc:
        raise CalculatorUnavailable(str(exc)) from exc


def _websocket_connector() -> Callable[..., Any]:
    """Load the maintained bounded WebSocket implementation only when needed."""
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise CalculatorUnavailable(
            "Online impedance calculation requires the plugin's websockets dependency. "
            "Install or update the complete plugin package."
        ) from exc
    return connect


def _tls_context() -> ssl.SSLContext:
    """Verify JLCPCB with the same CA bundle Requests uses under KiCad's Python.

    KiCad's embedded interpreter often has empty default OpenSSL verify paths, so
    ``ssl.create_default_context()`` alone fails certificate verification while
    HTTP catalog calls still succeed through certifi.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _records(
    response: dict[str, Any], container: str, label: str
) -> tuple[dict[str, Any], ...]:
    """Decode one known provider envelope while bounding shape and item count."""
    if response.get("success") is False or (
        response.get("result") != "success" and response.get("code") != 200
    ):
        raise CalculatorUnavailable(f"JLCPCB did not provide its {label}.")
    values = response.get(container)
    if isinstance(values, dict):
        values = values.get("list")
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 256
        or any(not isinstance(item, dict) for item in values)
    ):
        raise CalculatorUnavailable(
            f"JLCPCB's {label} format changed or is incomplete. Update the plugin."
        )
    return tuple(values)


def _manufacturing_row(
    records: tuple[dict[str, Any], ...], geometry: CrossSection
) -> dict[str, Any]:
    """Select the provider's exact copper-weight/layer-type manufacturing entry."""
    matches = []
    for record in records:
        if type(record.get("layerType")) is not int:
            raise CalculatorUnavailable(
                "JLCPCB returned an invalid manufacturing layer type."
            )
        if record["layerType"] != (1 if geometry.outer else 2):
            continue
        try:
            weight = positive_number(
                record.get("systemCopperThickness"), "provider copper weight"
            )
        except UnsupportedCalculation:
            continue
        if abs(weight - Decimal(str(geometry.copper_weight_oz))) < Decimal("0.01"):
            matches.append(record)
    if not matches:
        raise UnsupportedCalculation(
            "JLCPCB did not supply manufacturing parameters for this copper weight and layer type."
        )
    if len(matches) == 1:
        return matches[0]
    preferred = [
        item
        for item in matches
        if str(item.get("baseCopperThickness")) in ("0.5", ".5")
    ]
    if len(preferred) != 1:
        raise UnsupportedCalculation(
            "JLCPCB's copper construction is ambiguous; review it in the provider calculator."
        )
    return preferred[0]


def calculation_parameters(
    geometry: CrossSection, config: ProviderConfiguration
) -> dict[str, Any]:
    """Combine the cross-section with provider bounds, taper, copper, and soldermask."""
    records = [
        item for item in config.models if item.get("impedanceType") == geometry.model
    ]
    if geometry.model not in MODELS or len(records) != 1:
        raise UnsupportedCalculation(
            "JLCPCB did not supply a unique supported model for this construction."
        )
    parameters = records[0].get("parameterList")
    if not isinstance(parameters, list) or len(parameters) > 64:
        raise CalculatorUnavailable(
            "JLCPCB's impedance model parameters changed. Update the plugin."
        )
    values: dict[str, Any] = {}
    for item in parameters:
        if not isinstance(item, dict):
            raise CalculatorUnavailable("JLCPCB returned invalid model parameters.")
        name = item.get("paramName")
        if item.get("displayStatus") == 4:
            continue
        if not isinstance(name, str):
            raise CalculatorUnavailable("JLCPCB returned an invalid parameter name.")
        if name not in PARAMETERS or name in values:
            raise UnsupportedCalculation(
                "JLCPCB's model now requires unsupported or duplicate parameters."
            )
        values[name] = float(
            _provider_number(item.get("defaultValue"), "model parameter")
        )
    values.update(dict(geometry.values))
    copper = _manufacturing_row(config.copper, geometry)
    delta = float(_provider_number(copper.get("traceWidthDelta"), "trace taper"))
    values["T1"] = float(
        _provider_number(
            copper.get("traceCopperThickness"), "finished copper thickness"
        )
    )
    if geometry.outer:
        coating = _manufacturing_row(config.coating, geometry)
        for name, key in (
            ("C1", "coatingAboveSubstrate"),
            ("C2", "coatingAboveTrace"),
            ("C3", "coatingBetweenTraces"),
        ):
            if name in values:
                values[name] = float(
                    _provider_number(coating.get(key), "soldermask thickness")
                )
    limits = [item for item in config.limits if item.get("impedanceName") == "W2"]
    if len(limits) != 1:
        raise CalculatorUnavailable(
            "JLCPCB did not supply its width calculation bounds."
        )
    minimum = float(_provider_number(limits[0].get("minValue"), "minimum width"))
    maximum = float(_provider_number(limits[0].get("maxValue"), "maximum width"))
    if minimum >= maximum:
        raise CalculatorUnavailable(
            "JLCPCB returned inconsistent width calculation bounds."
        )
    if "W1" not in values or "W2" not in values:
        raise CalculatorUnavailable(
            "JLCPCB's model is missing its lower and upper width parameters."
        )
    # The inverse model solves W2 with the linked, fixed etch taper. The eventual
    # recommended KiCad width is the returned base width W1, not the etched W2.
    values["W1"] = max(values["W1"], minimum + delta)
    values["W2"] = values["W1"] - delta
    values.update(
        dCalculateMode=3,
        MinW2=minimum,
        MaxW2=maximum,
        W2LinkW1Incr=delta,
        isLinkComputingMode=False,
        ZoTol=0.5,
    )
    return values


class JlcpcbCalculator:
    """Run synchronous bounded network work inside a caller-owned background thread."""

    def __init__(
        self,
        *,
        post: Optional[Callable[..., dict[str, Any]]] = None,
        connector: Optional[Callable[..., Any]] = None,
        configuration: Optional[ProviderConfiguration] = None,
        save_configuration: Optional[Callable[[ProviderConfiguration], None]] = None,
    ) -> None:
        self._post = post or _default_post
        self._connector = connector
        self._configuration = configuration
        self._save_configuration = save_configuration
        self._lock = threading.Lock()

    def _request(
        self,
        endpoint: str,
        payload: dict[str, object],
        deadline: float,
        cancel: Optional[threading.Event],
    ) -> dict[str, Any]:
        """Limit every request by the remaining operation budget and owner cancellation."""
        timeout = min(15.0, _remaining(deadline, cancel))
        response = self._post(
            API_PREFIX + endpoint,
            payload,
            cancel=cancel.is_set if cancel is not None else None,
            timeout=timeout,
        )
        _remaining(deadline, cancel)
        if not isinstance(response, dict):
            raise CalculatorUnavailable(
                "JLCPCB returned an invalid calculator response."
            )
        try:
            _json(response)
        except (ValueError, TypeError, RecursionError) as exc:
            raise CalculatorUnavailable(
                "JLCPCB returned malformed or nonfinite calculator data."
            ) from exc
        return response

    def _load_configuration(
        self, deadline: float, cancel: Optional[threading.Event]
    ) -> ProviderConfiguration:
        """Reuse a fresh cached snapshot, otherwise fetch and optionally persist one."""
        if self._configuration is not None:
            _LOG.info("JLCPCB width: provider configuration already loaded in client")
            return self._configuration
        started = time.monotonic()
        _LOG.info("JLCPCB width: fetching provider configuration from JLCPCB")
        models = _records(
            self._request(
                "selectPageImpedancePicture",
                {"layerNumber": None, "pageNum": 1, "pageSize": 999, "usePurpose": 1},
                deadline,
                cancel,
            ),
            "body",
            "impedance models",
        )
        copper = _records(
            self._request(
                "impedance-config/copper-trace-width/list", {}, deadline, cancel
            ),
            "data",
            "copper manufacturing parameters",
        )
        coating = _records(
            self._request("impedance-config/coverlay/list", {}, deadline, cancel),
            "data",
            "soldermask parameters",
        )
        limits = _records(
            self._request(
                "selectImpedanceDefaultValue",
                {"pageNum": 1, "pageSize": 9999, "usePurpose": 1},
                deadline,
                cancel,
            ),
            "data",
            "width bounds",
        )
        result = ProviderConfiguration(models, copper, coating, limits)
        self._configuration = result
        _LOG.info(
            "JLCPCB width: provider configuration fetched in %.1fs",
            time.monotonic() - started,
        )
        if self._save_configuration is not None:
            with suppress(Exception):
                # Persistence failure must not discard a usable in-memory snapshot.
                self._save_configuration(result)
        return result

    def _solve(
        self,
        model: str,
        parameters: dict[str, Any],
        deadline: float,
        cancel: Optional[threading.Event],
    ) -> dict[str, Any]:
        """Subscribe before submitting, and accept only this request's model/result."""
        # Keep the plugin log window focused on our timing lines, not frame dumps.
        logging.getLogger("websockets").setLevel(logging.WARNING)
        connector = self._connector or _websocket_connector()
        client_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        mark = "W2_" + model
        request: dict[str, object] = {
            "accessId": request_id,
            "impedance_calc_mark": mark,
            "impedance_calc_arg": parameters,
            "paramMd5": hashlib.md5(
                _json(parameters).encode("utf-8"), usedforsecurity=False
            ).hexdigest(),
            "uuid": client_id,
        }
        connect_options: dict[str, Any] = {
            # This is a desktop client, not a page served by JLCPCB.
            "origin": None,
            "user_agent_header": "kicad-jlcpcb-tools/controlled-impedance",
            "open_timeout": min(10.0, _remaining(deadline, cancel)),
            "close_timeout": 1.0,
            "max_size": MAX_MESSAGE_BYTES,
            "max_queue": 4,
            "compression": None,
        }
        if WEBSOCKET_BASE.lower().startswith("wss:"):
            connect_options["ssl"] = _tls_context()
        try:
            with connector(WEBSOCKET_BASE + client_id, **connect_options) as connection:
                acknowledgement = self._request("calc", request, deadline, cancel)
                if (
                    acknowledgement.get("result") != "success"
                    or acknowledgement.get("success") is False
                ):
                    raise CalculatorUnavailable(
                        "JLCPCB did not accept the calculation."
                    )
                count = 0
                while count < MAX_MESSAGES:
                    remaining = _remaining(deadline, cancel)
                    try:
                        message = connection.recv(timeout=min(0.25, remaining))
                    except TimeoutError:
                        continue
                    _check_cancel(cancel)
                    count += 1
                    if (
                        not isinstance(message, (str, bytes))
                        or len(message) > MAX_MESSAGE_BYTES
                    ):
                        raise CalculatorUnavailable(
                            "JLCPCB returned an oversized or invalid calculator message."
                        )
                    try:
                        response = json.loads(message)
                        _json(response)
                    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
                        raise CalculatorUnavailable(
                            "JLCPCB returned an invalid calculator message."
                        ) from exc
                    if not isinstance(response, dict):
                        raise CalculatorUnavailable(
                            "JLCPCB returned an invalid calculator result."
                        )
                    if (
                        response.get("accessId") != request_id
                        or response.get("impedance_calc_mark") != mark
                    ):
                        continue
                    if (
                        type(response.get("impedance_calc_status")) is not int
                        or response["impedance_calc_status"] != 0
                    ):
                        detail = response.get("impedance_calc_error_msg")
                        if isinstance(detail, str) and detail.strip():
                            _LOG.info(
                                "JLCPCB width: provider rejection detail=%r",
                                " ".join(detail.split())[:300],
                            )
                        raise CalculatorUnavailable(_solver_rejection_message(response))
                    result = response.get("impedance_calc_result")
                    back = result.get("jBackCalc") if isinstance(result, dict) else None
                    if not isinstance(back, dict):
                        raise CalculatorUnavailable(
                            "JLCPCB returned an incomplete width result."
                        )
                    _remaining(deadline, cancel)
                    return back
                raise CalculatorUnavailable(
                    "JLCPCB returned too many unrelated calculator messages."
                )
        except (CalculatorUnavailable, CalculatorCancelled):
            raise
        except Exception as exc:
            _check_cancel(cancel)
            # WebSocket exceptions include protocol/handshake failures. Do not
            # expose arbitrary remote exception text or substitute a local result.
            raise CalculatorUnavailable(_websocket_failure_message(exc)) from exc

    def calculate_width(
        self,
        stackup: Stackup,
        spec: Specification,
        layer: str,
        *,
        cancel: Optional[threading.Event] = None,
    ) -> WidthResult:
        """Return a nominal base width or an explicit unavailable/unsupported state.

        Cancellation raises CalculatorCancelled. The UI must still reject a result
        whose input_digest no longer matches the active stackup/specification.
        """
        digest = calculation_fingerprint(stackup, spec, layer)
        deadline = time.monotonic() + MAX_TOTAL_SECONDS
        started = time.monotonic()
        _LOG.info(
            "JLCPCB width: solve start stackup=%s layer=%s kind=%s target=%sΩ",
            stackup.stackup_id,
            layer,
            spec.kind,
            spec.target_ohms,
        )
        _check_cancel(cancel)
        locked = False
        try:
            geometry = cross_section(stackup, spec, layer)
            # Missing dependency should fail before making metadata requests.
            if self._connector is None:
                _websocket_connector()
            while not locked:
                locked = self._lock.acquire(
                    timeout=min(0.25, _remaining(deadline, cancel))
                )
            config = self._load_configuration(deadline, cancel)
            parameters = calculation_parameters(geometry, config)
            _LOG.info(
                "JLCPCB width: websocket solve model=%s after %.1fs setup params=[%s]",
                geometry.model,
                time.monotonic() - started,
                _format_solve_parameters(parameters),
            )
            result = self._solve(geometry.model, parameters, deadline, cancel)
            width = _provider_number(result.get("W1"), "calculated base trace width")
            upper = _provider_number(result.get("W2"), "calculated upper trace width")
            if (
                not Decimal(str(parameters["MinW2"]))
                <= upper
                <= Decimal(str(parameters["MaxW2"]))
            ):
                raise CalculatorUnavailable(
                    "JLCPCB returned a width outside its calculation bounds."
                )
            if abs(width - upper - Decimal(str(parameters["W2LinkW1Incr"]))) > Decimal(
                "0.05"
            ):
                raise CalculatorUnavailable(
                    "JLCPCB returned inconsistent upper/base trace widths."
                )
            normalized_result = {"W1": str(width), "W2": str(upper)}
            for name in PARAMETERS.difference(("W1", "W2")):
                if name not in result:
                    continue
                actual = _provider_number(result[name], "returned fixed geometry")
                if name not in parameters:
                    raise CalculatorUnavailable(
                        "JLCPCB returned an unexpected fixed geometry parameter."
                    )
                expected = Decimal(str(parameters[name]))
                # This only allows numerical representation noise, not a design
                # tolerance or changes from the provider's width/gap complement.
                if abs(actual - expected) > max(
                    abs(expected) * Decimal("1e-10"), Decimal("1e-10")
                ):
                    raise CalculatorUnavailable(
                        "JLCPCB changed fixed spacing, clearance, or construction parameters."
                    )
                normalized_result[name] = str(actual)
            target_nm = int(
                (width * Decimal(25400)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            if target_nm <= 0:
                raise CalculatorUnavailable(
                    "JLCPCB returned a width below the supported dimension resolution."
                )
            _check_cancel(cancel)
            construction = {
                "nominal_signal_copper_mil": geometry.nominal_copper_mil,
                "finished_trace_copper_mil": parameters["T1"],
                "inner_H2_includes_nominal_copper": not geometry.outer,
            }
            assumptions = list(geometry.assumptions)
            if not geometry.outer:
                assumptions.append(
                    "Following JLCPCB's input convention, H2 includes the stackup's "
                    f"nominal copper ({geometry.nominal_copper_mil:.8g} mil); "
                    f"T1 uses its finished-trace model ({parameters['T1']:.8g} mil)."
                )
            _LOG.info(
                "JLCPCB width: solve success in %.1fs layer=%s width_nm=%s model=%s",
                time.monotonic() - started,
                layer,
                target_nm,
                geometry.model,
            )
            return WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="success",
                target_width_nm=target_nm,
                calculated_at_utc=utc_now(),
                model=geometry.model,
                assumptions=tuple(assumptions),
                calculation_digest=solver_fingerprint(
                    parameters, normalized_result, construction
                ),
                message="Nominal width only; reference-plane continuity and actual gaps are not verified.",
            )
        except CalculatorCancelled:
            _LOG.info(
                "JLCPCB width: solve cancelled after %.1fs layer=%s",
                time.monotonic() - started,
                layer,
            )
            raise
        except UnsupportedCalculation as exc:
            _LOG.info(
                "JLCPCB width: solve unsupported after %.1fs layer=%s: %s",
                time.monotonic() - started,
                layer,
                exc,
            )
            return WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="unsupported",
                message=str(exc),
            )
        except CalculatorUnavailable as exc:
            _LOG.info(
                "JLCPCB width: solve unavailable after %.1fs layer=%s: %s",
                time.monotonic() - started,
                layer,
                exc,
            )
            return WidthResult(
                spec_id=spec.spec_id,
                layer=layer,
                input_digest=digest,
                status="unavailable",
                message=str(exc),
            )
        finally:
            if locked:
                self._lock.release()
