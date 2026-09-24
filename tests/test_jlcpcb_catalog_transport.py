"""Catalog regressions derived from six-layer loading and connection failures.

These tests never contact JLCPCB or consult the user's proxy/netrc settings.
"""

from collections.abc import Callable
from decimal import Decimal
from email.utils import formatdate
import http.client
import io
import json
import socket
import ssl
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import requests
from urllib3 import exceptions as transport_errors

from impedance import jlcpcb_stackups as catalog
from impedance.jlcpcb_stackups import (
    CALCULATOR_PATH,
    CatalogError,
    _calculator_layers,
    post_json,
)


@pytest.mark.parametrize(
    ("conductor_thickness", "expected_thickness"),
    [("0", "0.865"), ("0.0152", "0.8954")],
)
def test_bare_board_is_one_dielectric_without_phantom_copper(
    conductor_thickness: str, expected_thickness: str
) -> None:
    """Provider type 3 must not prevent a complete six-layer catalog loading."""

    def material(
        kind: int, name: Optional[str] = None, **overrides: Any
    ) -> dict[str, Any]:
        return {
            "materialType": kind,
            "materialName": "FR4" if kind != 1 else "Copper",
            "layerName": name,
            "dielectricThick": "0.1",
            "dielectricConstant": "4.55",
            "topConductorThick": "0.035",
            "botConductorThick": "0.035",
            **overrides,
        }

    layers = _calculator_layers(
        {
            "basicDataList": [
                material(1, "L1"),
                material(0),
                material(2, "L2/L3"),
                material(0),
                material(
                    3,
                    dielectricThick="0.865",
                    topConductorThick=conductor_thickness,
                    botConductorThick=conductor_thickness,
                ),
                material(0),
                material(2, "L4/L5"),
                material(0),
                material(1, "L6"),
            ]
        },
        6,
    )

    assert [layer.name for layer in layers if layer.kind == "copper"] == [
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
        "L6",
    ]
    bare = [layer for layer in layers if layer.kind == "dielectric"]
    assert len(bare) == 1
    assert Decimal(bare[0].thickness_mm) == Decimal(expected_thickness)
    assert bare[0].dielectric_constant == "4.55"
    assert bare[0].material == "FR4"


class _ResponseBody(io.BytesIO):
    """Support urllib3's single-read interface without a socket."""

    def read1(self, size: int = -1, decode_content: bool = False) -> bytes:
        return super().read1(size)


def test_transport_honors_proxy_and_ca_without_implicit_origin_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Requests' real prepare/merge path, replacing only socket send."""
    captured: dict[str, Any] = {}
    netrc_lookups: list[str] = []

    def netrc_auth(url: str, raise_errors: bool = False) -> tuple[str, str]:
        netrc_lookups.append(url)
        return ("must-not-send", "must-not-send")

    def environment_proxies(url: str, no_proxy: Any = None) -> dict[str, str]:
        return {"https": "http://configured-proxy.invalid:8080"}

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            captured.update(kwargs)
            captured["headers"] = dict(request.headers)
            response = requests.Response()
            response.status_code = 200
            response.raw = _ResponseBody(b'{"success": true}')
            response.request = request
            return response

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc_auth)
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", environment_proxies)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/configured/corporate-ca.pem")
    monkeypatch.setattr(requests, "Session", Session)

    assert post_json(CALCULATOR_PATH, {}) == {"success": True}
    assert captured["proxies"]["https"] == "http://configured-proxy.invalid:8080"
    assert captured["verify"] == "/configured/corporate-ca.pem"
    assert captured["allow_redirects"] is False
    assert "Authorization" not in captured["headers"]
    assert "Origin" not in captured["headers"]
    assert "Referer" not in captured["headers"]
    assert (
        captured["headers"]["User-Agent"] == "kicad-jlcpcb-tools/controlled-impedance"
    )
    assert netrc_lookups == []


@pytest.mark.parametrize(
    ("error", "expected_category"),
    [
        (
            requests.exceptions.SSLError(
                ssl.SSLCertVerificationError(1, "private-connection-detail")
            ),
            "certificate",
        ),
        (
            requests.exceptions.ProxyError("private-connection-detail"),
            "proxy",
        ),
        (
            requests.exceptions.ConnectionError(
                socket.gaierror(-2, "private-connection-detail")
            ),
            "dns",
        ),
        (
            requests.exceptions.ReadTimeout("private-connection-detail"),
            "read timeout",
        ),
    ],
)
def test_transport_errors_are_actionable_without_sensitive_details(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
    expected_category: str,
) -> None:
    """The dialog should not reduce every transport failure to check connection."""

    def environment_proxies(url: str, no_proxy: Optional[str] = None) -> dict[str, str]:
        return {}

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            raise error

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", environment_proxies)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {})
    message = str(failure.value).lower()
    assert expected_category in message
    assert "private-connection-detail" not in message
    assert "unchanged" in message


class _Clock:
    """Advance deadlines and retry waits without network traffic or real sleeps."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []
        self.after_sleep: Optional[Callable[[], None]] = None

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.elapsed += duration
        if self.after_sleep is not None:
            self.after_sleep()

    def advance(self, duration: float) -> None:
        self.elapsed += duration


def _reply(
    status: int = 200,
    *,
    headers: Optional[dict[str, str]] = None,
    content: bytes = b'{"success": true}',
) -> requests.Response:
    """Use Requests' real response closing and the existing streaming interface."""
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response.raw = _ResponseBody(content)
    return response


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    clock: _Clock,
) -> list[dict[str, Any]]:
    """Replace socket send while retaining Requests preparation and cleanup."""
    calls: list[dict[str, Any]] = []

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            calls.append({"request": request, **kwargs})
            assert outcomes, "Unexpected extra request"
            outcome = outcomes.pop(0)
            if isinstance(outcome, tuple):
                duration, outcome = outcome
                clock.advance(duration)
            if isinstance(outcome, BaseException):
                raise outcome
            outcome.request = request
            return outcome

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(
        requests.sessions, "get_environ_proxies", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        catalog,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic, sleep=clock.sleep, time=clock.monotonic
        ),
    )
    monkeypatch.setattr(
        catalog, "random", SimpleNamespace(uniform=lambda low, high: 0.0), raising=False
    )
    return calls


def test_catalog_request_recovers_without_changing_the_requested_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout and reset should be invisible after bounded eventual success."""
    clock = _Clock()
    response = _reply()
    calls = _transport(
        monkeypatch,
        [requests.ReadTimeout("private-detail"), ConnectionResetError(), response],
        clock,
    )
    payload = {"pageNum": 2, "plateLayerNumber": 6}

    assert post_json(CALCULATOR_PATH, payload, attempts=3) == {"success": True}
    assert len(calls) == 3
    assert clock.elapsed == pytest.approx(3.0)
    assert [json.loads(call["request"].body) for call in calls] == [payload] * 3
    assert {call["request"].url for call in calls} == {
        catalog.API_ROOT + CALCULATOR_PATH
    }
    assert response.raw.closed


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_failures_are_bounded_and_close_every_response(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Retry server overload briefly, then expose a typed temporary failure."""
    clock = _Clock()
    responses = [_reply(status) for _ in range(3)]
    calls = _transport(monkeypatch, list(responses), clock)

    with pytest.raises(catalog.CatalogUnavailable) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)

    assert len(calls) == 3
    assert clock.elapsed == pytest.approx(3.0)
    assert str(status) in str(failure.value)
    assert failure.value.retry_after == 0.0
    assert all(response.raw.closed for response in responses)


@pytest.mark.parametrize(
    "error",
    [
        requests.ReadTimeout("private-connection-detail"),
        requests.ConnectTimeout("private-connection-detail"),
        requests.ConnectionError(socket.gaierror(-2, "private-connection-detail")),
        ConnectionResetError("private-connection-detail"),
        requests.exceptions.ChunkedEncodingError("private-connection-detail"),
    ],
)
def test_transient_transport_exhaustion_stays_sanitized(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Transient categories retain safe diagnostic text after exhausting retries."""
    clock = _Clock()
    calls = _transport(monkeypatch, [error] * 3, clock)
    with pytest.raises(catalog.CatalogUnavailable) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert len(calls) == 3
    assert "private-connection-detail" not in str(failure.value)
    assert "unchanged" in str(failure.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 407, 501])
def test_permanent_http_rejections_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Auth and unsupported provider responses need intervention, not repetition."""
    clock = _Clock()
    response = _reply(status)
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert not isinstance(failure.value, catalog.CatalogUnavailable)
    assert len(calls) == 1
    assert clock.sleeps == []
    assert response.raw.closed


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, "private-detail")),
        requests.exceptions.SSLError("private-detail"),
        requests.exceptions.ProxyError("private-detail"),
        requests.exceptions.InvalidProxyURL("private-detail"),
        requests.exceptions.InvalidSchema("private-detail"),
        OSError("private-detail"),
    ],
)
def test_tls_proxy_and_configuration_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Certificate verification and explicit proxy failures remain visible."""
    clock = _Clock()
    calls = _transport(monkeypatch, [error], clock)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert not isinstance(failure.value, catalog.CatalogUnavailable)
    assert "private-detail" not in str(failure.value)
    assert len(calls) == 1
    assert clock.sleeps == []


def _proxy_failure(cause: Exception) -> requests.exceptions.ProxyError:
    """Match installed urllib3's proxy wrapping and Requests' adapter boundary."""
    proxy_error = transport_errors.ProxyError("private-proxy-detail", cause)
    retry_error = transport_errors.MaxRetryError(
        None, "https://private.invalid/", proxy_error
    )
    return requests.exceptions.ProxyError(retry_error)


@pytest.mark.parametrize(
    "cause",
    [
        transport_errors.ConnectTimeoutError("private-detail"),
        ConnectionResetError("private-detail"),
        socket.gaierror(-2, "private-detail"),
        http.client.IncompleteRead(b"private-detail"),
        transport_errors.NewConnectionError(None, "private-detail"),
    ],
)
def test_known_transient_proxy_causes_recover_without_exposing_proxy_details(
    monkeypatch: pytest.MonkeyPatch, cause: Exception
) -> None:
    """Using a configured proxy must retain retries for underlying interruptions."""
    clock = _Clock()
    error = _proxy_failure(cause)
    response = _reply()
    calls = _transport(monkeypatch, [error, response], clock)
    assert post_json(CALCULATOR_PATH, {}, attempts=3) == {"success": True}
    assert len(calls) == 2
    assert clock.elapsed == pytest.approx(1.0)
    assert response.raw.closed


@pytest.mark.parametrize(
    "cause",
    [
        OSError("Tunnel connection failed: 407 private-detail"),
        ssl.SSLCertVerificationError(1, "private-detail"),
        requests.exceptions.InvalidProxyURL("private-detail"),
        requests.exceptions.InvalidSchema("private-detail"),
    ],
)
def test_proxy_auth_certificate_and_configuration_causes_remain_exceptional(
    monkeypatch: pytest.MonkeyPatch, cause: Exception
) -> None:
    """Proxy wrappers must not conceal trust, unsupported transport or auth errors."""
    clock = _Clock()
    calls = _transport(monkeypatch, [_proxy_failure(cause)], clock)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert not isinstance(failure.value, catalog.CatalogUnavailable)
    assert len(calls) == 1
    assert clock.sleeps == []
    assert "private" not in str(failure.value)


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({}, b"not JSON"),
        ({}, b"[]"),
        ({}, b'{"number": NaN}'),
        ({"Content-Encoding": "gzip"}, b"{}"),
        ({"Content-Length": "invalid"}, b"{}"),
    ],
)
def test_invalid_provider_responses_fail_without_retry(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], content: bytes
) -> None:
    """A retry must not conceal provider schema or invalid-JSON changes."""
    clock = _Clock()
    response = _reply(headers=headers, content=content)
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(CatalogError) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert not isinstance(failure.value, catalog.CatalogUnavailable)
    assert len(calls) == 1
    assert response.raw.closed


@pytest.mark.parametrize("as_date", [False, True])
def test_retry_after_delay_is_honored_before_retry(
    monkeypatch: pytest.MonkeyPatch, as_date: bool
) -> None:
    """The server's relative delay and HTTP-date hint have equal authority."""
    clock = _Clock()
    retry_after = formatdate(4.0, usegmt=True) if as_date else "4"
    first, second = _reply(429, headers={"Retry-After": retry_after}), _reply()
    calls = _transport(monkeypatch, [first, second], clock)
    assert post_json(CALCULATOR_PATH, {}, attempts=3) == {"success": True}
    assert len(calls) == 2
    assert clock.elapsed == pytest.approx(4.0)
    assert first.raw.closed and second.raw.closed


@pytest.mark.parametrize("hint", ["invalid", "-1", "1.5", ""])
def test_invalid_retry_hint_uses_normal_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch, hint: str
) -> None:
    """Malformed server metadata does not cause a crash or immediate retry loop."""
    clock = _Clock()
    calls = _transport(
        monkeypatch, [_reply(503, headers={"Retry-After": hint}), _reply()], clock
    )
    assert post_json(CALCULATOR_PATH, {}, attempts=3) == {"success": True}
    assert len(calls) == 2
    assert clock.elapsed == pytest.approx(1.0)


def test_provider_delay_beyond_deadline_is_preserved_without_retrying_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long Retry-After is returned intact for the cross-dialog cooldown."""
    clock = _Clock()
    response = _reply(503, headers={"Retry-After": "900"})
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(catalog.CatalogUnavailable) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert len(calls) == 1
    assert clock.sleeps == []
    assert failure.value.retry_after == pytest.approx(900.0)
    assert response.raw.closed


def test_retry_attempts_share_one_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retry receives the remaining budget rather than a fresh timeout."""
    clock = _Clock()
    first, second = _reply(503), _reply(503)
    calls = _transport(monkeypatch, [(21.0, first), (1.0, second)], clock)
    with pytest.raises(catalog.CatalogUnavailable):
        post_json(CALCULATOR_PATH, {}, attempts=3, timeout=25.0)
    assert len(calls) == 2
    assert [call["timeout"].total for call in calls] == pytest.approx([25.0, 3.0])
    assert clock.elapsed <= 25.0
    assert first.raw.closed and second.raw.closed


def test_cancellation_interrupts_backoff_without_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the owner interrupts the delay in at most one short wait slice."""
    clock = _Clock()
    calls = _transport(monkeypatch, [requests.ReadTimeout()], clock)
    cancelled = False

    def cancel_after_first_sleep() -> None:
        nonlocal cancelled
        cancelled = True

    clock.after_sleep = cancel_after_first_sleep
    with pytest.raises(catalog.CatalogCancelled):
        post_json(CALCULATOR_PATH, {}, attempts=3, cancel=lambda: cancelled)
    assert len(calls) == 1
    assert clock.elapsed <= 0.1


def test_deadline_expiring_between_backoff_samples_does_not_sleep_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduling pause during backoff still produces a temporary timeout."""
    clock = _Clock()
    _transport(monkeypatch, [], clock)
    samples = iter([0.5, 0.9, 1.1])
    monkeypatch.setattr(catalog.time, "monotonic", lambda: next(samples, 1.1))

    def sleep(duration: float) -> None:
        if duration < 0:
            raise ValueError("sleep length must be non-negative")
        clock.sleep(duration)

    monkeypatch.setattr(catalog.time, "sleep", sleep)
    with pytest.raises(catalog.CatalogUnavailable):
        catalog._wait_for_retry(1.0, None, 1.0)
    assert clock.sleeps == []


def test_cancellation_wins_when_backoff_budget_expires_between_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closure during the sampled-budget boundary cannot publish a timeout."""
    clock = _Clock()
    _transport(monkeypatch, [], clock)
    cancelled = False
    samples = iter([0.5, 0.9, 1.1])

    def monotonic() -> float:
        nonlocal cancelled
        value = next(samples, 1.1)
        if value >= 1.1:
            cancelled = True
        return value

    monkeypatch.setattr(catalog.time, "monotonic", monotonic)
    with pytest.raises(catalog.CatalogCancelled):
        catalog._wait_for_retry(1.0, lambda: cancelled, 1.0)
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [403, 503])
def test_cancellation_wins_over_response_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A late response cannot publish either an error or retry after closure."""
    clock = _Clock()
    response = _reply(status)
    calls = _transport(monkeypatch, [(1.0, response)], clock)
    with pytest.raises(catalog.CatalogCancelled):
        post_json(CALCULATOR_PATH, {}, attempts=3, cancel=lambda: clock.elapsed >= 1)
    assert len(calls) == 1
    assert response.raw.closed


def test_calculator_submission_does_not_implicitly_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calculator submission could have side effects despite a lost reply."""
    clock = _Clock()
    response = _reply(503)
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(CatalogError):
        post_json("/jlcTools/impedance/calc", {})
    assert len(calls) == 1
    assert clock.sleeps == []


def test_both_catalog_sources_opt_in_to_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering and calculator catalog reads use the same bounded retry policy."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append((path, kwargs))
        if path == CALCULATOR_PATH:
            return {
                "result": "success",
                "body": {"total": 0, "pageNum": 1, "pageSize": 100, "list": []},
            }
        return {"success": True, "code": 200, "data": []}

    monkeypatch.setattr(catalog, "post_json", post)
    assert catalog.fetch_stackups(4) == ()
    assert [path for path, _ in calls] == [CALCULATOR_PATH, catalog.ORDER_PATH]
    assert [kwargs.get("attempts") for _, kwargs in calls] == [3, 3]
    assert all(0 < kwargs["timeout"] <= 25 for _, kwargs in calls)


def test_page_failure_retries_that_page_without_restarting_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed later page is retried with its own payload and prior rows intact."""
    clock = _Clock()
    rows = [{"impedanceDefaultTemplateAccessId": str(index)} for index in range(101)]

    def page(number: int, items: list[dict[str, Any]]) -> requests.Response:
        return _reply(
            content=json.dumps(
                {
                    "result": "success",
                    "body": {
                        "total": 101,
                        "pageNum": number,
                        "pageSize": 100,
                        "list": items,
                    },
                }
            ).encode()
        )

    responses = [page(1, rows[:100]), _reply(503), page(2, rows[100:])]
    calls = _transport(monkeypatch, list(responses), clock)
    assert catalog._calculator_records(4, None, 120.0) == rows
    assert [json.loads(call["request"].body)["pageNum"] for call in calls] == [1, 2, 2]
    assert all(response.raw.closed for response in responses)


def test_incomplete_native_read_recovers_and_closes_the_partial_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted urllib3/native stream must not leak a response connection."""
    clock = _Clock()

    class Body(_ResponseBody):
        def read1(self, size: int = -1, decode_content: bool = False) -> bytes:
            raise http.client.IncompleteRead(b"private-detail")

    first, second = _reply(), _reply()
    first.raw = Body(b"{}")
    calls = _transport(monkeypatch, [first, second], clock)
    assert post_json(CALCULATOR_PATH, {}, attempts=3) == {"success": True}
    assert len(calls) == 2
    assert first.raw.closed and second.raw.closed


def test_retry_hint_reports_remaining_delay_after_response_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response teardown time counts towards the provider's requested delay."""
    clock = _Clock()

    class Body(_ResponseBody):
        def close(self) -> None:
            clock.advance(0.5)
            super().close()

    response = _reply(503, headers={"Retry-After": "900"})
    response.raw = Body(b"{}")
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(catalog.CatalogUnavailable) as failure:
        post_json(CALCULATOR_PATH, {}, attempts=3)
    assert len(calls) == 1
    assert failure.value.retry_after == pytest.approx(899.5)


def test_cancellation_during_decode_prevents_publishing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final publication boundary still belongs to the requesting dialog."""
    clock = _Clock()
    _transport(monkeypatch, [_reply()], clock)
    cancelled = False
    decode = json.loads

    def cancelling_decode(*args: Any, **kwargs: Any) -> Any:
        nonlocal cancelled
        cancelled = True
        return decode(*args, **kwargs)

    monkeypatch.setattr(catalog, "json", SimpleNamespace(loads=cancelling_decode))
    with pytest.raises(catalog.CatalogCancelled):
        post_json(CALCULATOR_PATH, {}, attempts=3, cancel=lambda: cancelled)


def test_streaming_response_is_stopped_at_the_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow progress cannot extend a logical catalog request indefinitely."""
    clock = _Clock()

    class Body(_ResponseBody):
        def read1(self, size: int = -1, decode_content: bool = False) -> bytes:
            clock.advance(5.0)
            return super().read1(1)

    response = _reply()
    response.raw = Body(b'{"success": true}')
    calls = _transport(monkeypatch, [response], clock)
    with pytest.raises(catalog.CatalogUnavailable):
        post_json(CALCULATOR_PATH, {}, attempts=3, timeout=25.0)
    assert len(calls) == 1
    assert clock.elapsed == 25.0
    assert response.raw.closed


def test_catalog_pages_and_retries_share_the_catalog_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new page cannot reset the complete catalog's 120-second time limit."""
    clock = _Clock()
    responses: list[requests.Response] = []
    outcomes: list[Any] = []
    for page in range(1, 5):
        response = _reply(
            content=json.dumps(
                {
                    "result": "success",
                    "body": {
                        "total": 501,
                        "pageNum": page,
                        "pageSize": 100,
                        "list": [
                            {"impedanceDefaultTemplateAccessId": f"{page}-{index}"}
                            for index in range(100)
                        ],
                    },
                }
            ).encode()
        )
        responses.append(response)
        outcomes.append((24.0, response))
    responses.extend([_reply(503), _reply(503)])
    outcomes.extend([(20.0, responses[-2]), (3.0, responses[-1])])
    calls = _transport(monkeypatch, outcomes, clock)
    with pytest.raises(catalog.CatalogUnavailable):
        catalog.fetch_stackups(4)
    assert len(calls) == 6
    assert clock.elapsed == pytest.approx(120.0)
    assert [call["timeout"].total for call in calls] == pytest.approx(
        [25.0, 25.0, 25.0, 25.0, 24.0, 3.0]
    )
    assert [json.loads(call["request"].body)["pageNum"] for call in calls] == [
        1,
        2,
        3,
        4,
        5,
        5,
    ]
    assert all(response.raw.closed for response in responses)


@pytest.mark.parametrize("source", ["calculator", "ordering"])
def test_deadline_expiring_while_sampling_timeout_remains_temporary(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """A budget boundary cannot become an exceptional invalid-timeout error."""
    clock = _Clock()
    calls = _transport(monkeypatch, [], clock)
    samples = iter([119.9, 120.1] if source == "calculator" else [0.0, 119.9, 120.1])
    monkeypatch.setattr(catalog.time, "monotonic", lambda: next(samples, 120.1))
    if source == "ordering":
        monkeypatch.setattr(catalog, "_calculator_records", lambda *args: [])
    with pytest.raises(catalog.CatalogUnavailable) as failure:
        if source == "calculator":
            catalog._calculator_records(4, None, 120.0)
        else:
            catalog.fetch_stackups(4)
    assert "timed out" in str(failure.value)
    assert calls == []


def test_retries_keep_requests_anonymous_despite_provider_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying a public read should not acquire a provider session identity."""
    clock = _Clock()
    requests_seen: list[requests.PreparedRequest] = []

    class Session(requests.Session):
        def send(
            self, request: requests.PreparedRequest, **kwargs: Any
        ) -> requests.Response:
            requests_seen.append(request)
            self.cookies.set("provider-session", "must-not-send")
            return _reply(503 if len(requests_seen) == 1 else 200)

    _transport(monkeypatch, [], clock)
    monkeypatch.setattr(requests, "Session", Session)
    assert post_json(CALCULATOR_PATH, {}, attempts=3) == {"success": True}
    assert len(requests_seen) == 2
    assert all("Cookie" not in request.headers for request in requests_seen)
