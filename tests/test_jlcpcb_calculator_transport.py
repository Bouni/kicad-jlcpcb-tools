"""Exercise the calculator's real WebSocket handshake with anonymous sample inputs.

Handshake and result tests use deterministic HTTP fixtures plus the shipped
WebSocket client and a loopback server. Deadline and cancellation-race tests use
connection doubles. No test contacts JLCPCB or opens production board files.
"""

from collections.abc import Iterator
from copy import deepcopy
from functools import partial
import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from impedance import jlcpcb_calculator as calculator
from impedance.model import LayerSettings, Specification
from impedance.stackup_model import Stackup, StackupLayer

# Import after the package bootstrap makes the shipped dependencies available.
# isort: split
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.client import connect
from websockets.sync.server import ServerConnection, serve


def _inputs() -> tuple[Stackup, Specification]:
    """Describe a synthetic two-layer microstrip without opening board files."""
    return (
        Stackup(
            stackup_id="sample-construction",
            name="Sample construction",
            layer_count=2,
            thickness_mm="0.324",
            outer_copper_oz="1",
            inner_copper_oz="",
            calculator_id="sample-calculator",
            layers=(
                StackupLayer("L1", "copper", "0.035"),
                StackupLayer("Core", "core", "0.254", "FR4", "4.2"),
                StackupLayer("L2", "copper", "0.035"),
            ),
        ),
        Specification(
            spec_id="private-spec-id",
            label="Private label",
            target_ohms="50",
            kind="single_ended",
            net_class="Private net class",
            layer_settings=(LayerSettings("F.Cu", ("B.Cu",)),),
        ),
    )


class _Provider:
    """Supply provider metadata and deliver submitted calculations over a socket."""

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.connection: Optional[ServerConnection] = None
        self.headers: dict[str, str] = {}
        self.submissions: list[dict[str, Any]] = []
        self.mode = "success"
        self.cancel: Optional[threading.Event] = None
        self.cancelled_at = 0.0
        self.timer: Optional[threading.Timer] = None

    def handshake(
        self, connection: ServerConnection, request: Request
    ) -> Optional[Response]:
        """Observe actual wire headers and optionally refuse the handshake."""
        self.headers = dict(request.headers)
        if self.mode == "handshake-rejected":
            return connection.respond(403, "Not accepted")
        return None

    def handle(self, connection: ServerConnection) -> None:
        """Keep the subscription open until the worker closes its connection."""
        self.connection = connection
        self.connected.set()
        try:
            connection.recv(timeout=5.0)
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            self.closed.set()

    def post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Use independent sample manufacturing inputs and the real submit path."""
        assert 0 < kwargs["timeout"] <= 15.0
        endpoint = path.removeprefix(calculator.API_PREFIX)
        if endpoint == "selectPageImpedancePicture":
            return {
                "result": "success",
                "body": [
                    {
                        "impedanceType": "CoatedMicrostrip1B",
                        "parameterList": [
                            {"paramName": name, "defaultValue": value}
                            for name, value in {
                                "W1": 8,
                                "W2": 7.5,
                                "H1": 10,
                                "Er1": 4.2,
                                "T1": 1.4,
                                "C1": 0.5,
                                "C2": 0.5,
                                "CEr": 3.5,
                            }.items()
                        ],
                    }
                ],
            }
        rows = {
            "impedance-config/copper-trace-width/list": {
                "layerType": 1,
                "systemCopperThickness": 1,
                "traceWidthDelta": 0.5,
                "traceCopperThickness": 1.4,
            },
            "impedance-config/coverlay/list": {
                "layerType": 1,
                "systemCopperThickness": 1,
                "coatingAboveSubstrate": 0.5,
                "coatingAboveTrace": 0.5,
            },
            "selectImpedanceDefaultValue": {
                "impedanceName": "W2",
                "minValue": 0.5,
                "maxValue": 100,
            },
        }
        if endpoint in rows:
            return {"code": 200, "data": [rows[endpoint]]}
        assert endpoint == "calc"
        assert self.connected.wait(1.0), "Submission must follow the subscription"
        self.submissions.append(deepcopy(payload))
        if self.mode == "submission-rejected":
            return {"result": "failed", "success": False}
        if self.mode == "silent":
            return {"result": "success"}
        if self.mode == "cancel":
            assert self.cancel is not None
            self.timer = threading.Timer(0.05, self.cancel_owner)
            self.timer.start()
            return {"result": "success"}
        self.reply(payload)
        return {"result": "success"}

    def cancel_owner(self) -> None:
        """Measure cancellation after subscription, excluding real setup time."""
        assert self.cancel is not None
        self.cancelled_at = time.monotonic()
        self.cancel.set()

    def reply(self, payload: dict[str, Any]) -> None:
        """Send unrelated messages before the correlated result to test isolation."""
        assert self.connection is not None
        if self.mode in ("invalid-json", "oversized"):
            size = calculator.MAX_MESSAGE_BYTES + 1 if self.mode == "oversized" else 1
            self.connection.send("!" * size)
            return
        response = {
            "accessId": payload["accessId"],
            "impedance_calc_mark": payload["impedance_calc_mark"],
            "impedance_calc_status": 0,
            "impedance_calc_result": {"jBackCalc": {"W1": 8.5, "W2": 8.0}},
        }
        for override in (
            {"accessId": "another-request"},
            {"impedance_calc_mark": "W2_AnotherModel"},
        ):
            self.connection.send(
                json.dumps({**response, **override, "impedance_calc_status": 1})
            )
        if self.mode == "solver-rejected":
            response["impedance_calc_status"] = 1
            response["impedance_calc_error_msg"] = "SI9000计算阻抗失败"
        elif self.mode == "changed-geometry":
            response["impedance_calc_result"]["jBackCalc"]["H1"] = 11
        elif self.mode == "invalid-width":
            response["impedance_calc_result"]["jBackCalc"]["W1"] = "NaN"
        elif self.mode == "unrelated-flood":
            response["accessId"] = "another-request"
            for _ in range(calculator.MAX_MESSAGES):
                try:
                    self.connection.send(json.dumps(response))
                except ConnectionClosed:
                    break
            return
        self.connection.send(json.dumps(response))


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Provider]:
    """Run an ephemeral loopback WebSocket server with bounded teardown."""
    instance = _Provider()
    with serve(
        instance.handle,
        "127.0.0.1",
        0,
        process_request=instance.handshake,
        close_timeout=0.5,
    ) as server:
        port = server.socket.getsockname()[1]
        monkeypatch.setattr(calculator, "WEBSOCKET_BASE", f"ws://127.0.0.1:{port}/")
        # Ignore user proxy settings for loopback only. All other production
        # connect arguments and the bundled client execute without substitution.
        monkeypatch.setattr(
            calculator, "_websocket_connector", lambda: partial(connect, proxy=None)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield instance
        finally:
            if instance.timer is not None:
                instance.timer.cancel()
                instance.timer.join(timeout=1.0)
            if instance.connection is not None:
                instance.connection.close()
            server.shutdown()
            thread.join(timeout=2.0)
            assert not thread.is_alive()


@pytest.mark.parametrize("header", ["origin", "user-agent"])
def test_handshake_identifies_a_desktop_client(
    provider: _Provider, header: str
) -> None:
    """A desktop plugin must not claim to be a page served by JLCPCB."""
    result = calculator.JlcpcbCalculator(post=provider.post).calculate_width(
        *_inputs(), "F.Cu"
    )
    assert result.status == "success"
    if header == "origin":
        assert "origin" not in provider.headers
    else:
        assert provider.headers[header] == "kicad-jlcpcb-tools/controlled-impedance"


def test_correlated_result_keeps_inputs_private_and_unchanged(
    provider: _Provider,
) -> None:
    """Only this subscription's result produces a nominal width and fingerprint."""
    inputs = _inputs()
    before = deepcopy(inputs)
    result = calculator.JlcpcbCalculator(post=provider.post).calculate_width(
        *inputs, "F.Cu"
    )
    assert result.status == "success"
    assert result.target_width_nm == 215_900
    assert len(result.calculation_digest) == 64
    assert result.spec_id == inputs[1].spec_id
    assert inputs == before
    request = provider.submissions[0]
    assert request["accessId"] != request["uuid"]
    assert set(request) == {
        "accessId",
        "uuid",
        "impedance_calc_mark",
        "impedance_calc_arg",
        "paramMd5",
    }
    wire = json.dumps(request).lower()
    assert "private" not in wire and "sample" not in wire and "f.cu" not in wire
    assert provider.closed.wait(1.0)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("handshake-rejected", "Could not receive"),
        ("submission-rejected", "did not accept"),
        ("solver-rejected", "could not calculate a width"),
        ("changed-geometry", "changed fixed"),
        ("invalid-width", "invalid calculated"),
        ("unrelated-flood", "too many unrelated"),
        ("invalid-json", "invalid calculator message"),
        ("oversized", "closed the connection"),
    ],
)
def test_failure_does_not_publish_a_width_or_mutate_inputs(
    provider: _Provider, mode: str, message: str
) -> None:
    """Rejected or unusable results remain explicit failures without a fallback."""
    provider.mode = mode
    inputs = _inputs()
    before = deepcopy(inputs)
    result = calculator.JlcpcbCalculator(post=provider.post).calculate_width(
        *inputs, "F.Cu"
    )
    assert result.status == "unavailable"
    assert result.target_width_nm is None
    assert result.calculation_digest == ""
    assert message in result.message
    assert inputs == before
    if mode == "solver-rejected":
        assert result.message == "JLCPCB could not calculate a width"
        assert not any("\u4e00" <= char <= "\u9fff" for char in result.message)
    if mode == "handshake-rejected":
        assert provider.submissions == []
    else:
        assert provider.closed.wait(1.0)


def test_silent_provider_obeys_total_deadline_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advance only the worker's clock so CI speed cannot consume its setup budget."""
    now = [0.0]
    polls: list[float] = []
    closed: list[bool] = []

    class SilentConnection:
        def __enter__(self) -> "SilentConnection":
            return self

        def __exit__(self, *args: Any) -> None:
            closed.append(True)

        def recv(self, *, timeout: float) -> str:
            polls.append(timeout)
            now[0] += timeout
            raise TimeoutError

    monkeypatch.setattr(calculator, "time", SimpleNamespace(monotonic=lambda: now[0]))
    client = calculator.JlcpcbCalculator(
        connector=lambda *args, **kwargs: SilentConnection(),
        post=lambda *args, **kwargs: {"result": "success"},
    )
    with pytest.raises(calculator.CalculatorUnavailable, match="timed out"):
        client._solve("CoatedMicrostrip1B", {}, 0.4, None)
    assert polls == pytest.approx([0.25, 0.15])
    assert now[0] == pytest.approx(0.4)
    assert closed == [True]


def test_cancellation_during_receive_closes_without_a_failure_result(
    provider: _Provider,
) -> None:
    """Closing the owner interrupts the poll, and a later retry still works."""
    provider.mode = "cancel"
    provider.cancel = threading.Event()
    client = calculator.JlcpcbCalculator(post=provider.post)
    inputs = _inputs()
    before = deepcopy(inputs)
    with pytest.raises(calculator.CalculatorCancelled):
        client.calculate_width(*inputs, "F.Cu", cancel=provider.cancel)
    assert provider.cancelled_at > 0
    assert time.monotonic() - provider.cancelled_at < 2.5
    assert provider.closed.wait(1.0)
    assert inputs == before
    provider.mode = "success"
    provider.connected.clear()
    provider.closed.clear()
    result = client.calculate_width(*inputs, "F.Cu")
    assert result.status == "success"


@pytest.mark.parametrize("failure", ["connect", "receive", "invalid-json"])
def test_cancellation_wins_a_simultaneous_connection_failure(failure: str) -> None:
    """An abandoned operation must not turn a socket error into a saved failure."""
    cancel = threading.Event()

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def recv(self, *, timeout: float) -> str:
            cancel.set()
            if failure == "receive":
                raise OSError("connection closed while cancelling")
            return "malformed json"

    def fail_connect(*args: Any, **kwargs: Any) -> Connection:
        if failure == "connect":
            cancel.set()
            raise OSError("connection closed while cancelling")
        return Connection()

    client = calculator.JlcpcbCalculator(
        connector=fail_connect, post=lambda *args, **kwargs: {"result": "success"}
    )
    with pytest.raises(calculator.CalculatorCancelled):
        client._solve("CoatedMicrostrip1B", {}, time.monotonic() + 1.0, cancel)


def test_tls_context_requires_certificate_verification() -> None:
    """KiCad's empty default CA paths must not disable TLS verification."""
    import ssl

    context = calculator._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_wss_subscriptions_pass_an_explicit_ca_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secure sockets must use certifi; plaintext loopback tests must not."""
    import ssl

    seen: dict[str, Any] = {}
    now = [0.0]

    class SilentConnection:
        def __enter__(self) -> "SilentConnection":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def recv(self, *, timeout: float) -> str:
            now[0] += timeout
            raise TimeoutError

    def capture_connect(uri: str, **kwargs: Any) -> SilentConnection:
        seen["uri"] = uri
        seen["kwargs"] = kwargs
        return SilentConnection()

    monkeypatch.setattr(calculator, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(calculator, "WEBSOCKET_BASE", "wss://example.test/ws/")
    client = calculator.JlcpcbCalculator(
        connector=capture_connect,
        post=lambda *args, **kwargs: {"result": "success"},
    )
    with pytest.raises(calculator.CalculatorUnavailable, match="timed out"):
        client._solve("CoatedMicrostrip1B", {"H1": 1.0}, 0.4, None)
    assert seen["uri"].startswith("wss://example.test/ws/")
    assert isinstance(seen["kwargs"].get("ssl"), ssl.SSLContext)
    assert seen["kwargs"]["ssl"].verify_mode == ssl.CERT_REQUIRED

    seen.clear()
    now[0] = 0.0
    monkeypatch.setattr(calculator, "WEBSOCKET_BASE", "ws://127.0.0.1:9/")
    with pytest.raises(calculator.CalculatorUnavailable, match="timed out"):
        client._solve("CoatedMicrostrip1B", {"H1": 1.0}, 0.4, None)
    assert "ssl" not in seen["kwargs"]
