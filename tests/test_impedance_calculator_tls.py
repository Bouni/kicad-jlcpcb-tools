"""Exercise CA overrides with real TLS and the shipped loopback WebSocket transport."""

import asyncio
from collections.abc import Callable, Iterator
import logging
from pathlib import Path
import shutil
import ssl
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest

from impedance import jlcpcb_calculator as calculator
from impedance._vendor.websockets.asyncio.server import ServerConnection, serve
from impedance._vendor.websockets.http11 import Request, Response

from .test_jlcpcb_calculator_transport import _inputs, _Provider

FIXTURES = Path(__file__).parent / "fixtures" / "impedance_tls"


class _SecureSubscription:
    """Dispatch provider replies to the thread that owns the TLS connection."""

    def __init__(
        self, connection: ServerConnection, loop: asyncio.AbstractEventLoop
    ) -> None:
        self.connection, self.loop = connection, loop

    def send(self, message: str) -> None:
        asyncio.run_coroutine_threadsafe(
            self.connection.send(message), self.loop
        ).result(timeout=1)

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self.connection.close(), self.loop).result(
            timeout=1
        )


class _SecureProvider(_Provider):
    """Expose the owner loop for tests that drive real peer protocol events."""

    loop: asyncio.AbstractEventLoop


@pytest.fixture
def secure_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SecureProvider]:
    """Start a TLS subscription with certificates usable only for localhost tests."""
    instance = _SecureProvider()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server-key.pem")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    ready = threading.Event()
    failures: list[BaseException] = []
    loop: Optional[asyncio.AbstractEventLoop] = None
    stop: Optional[asyncio.Event] = None
    port = 0

    async def handle(connection: ServerConnection) -> None:
        instance.connection = _SecureSubscription(
            connection, asyncio.get_running_loop()
        )
        instance.connected.set()
        try:
            await connection.wait_closed()
        except BaseException as error:
            failures.append(error)
            raise
        finally:
            instance.closed.set()

    def handshake(connection: Any, request: Request) -> Optional[Response]:
        """Keep provider hooks observable without swallowing callback failures."""
        try:
            return instance.handshake(connection, request)
        except BaseException as error:
            failures.append(error)
            raise

    async def run() -> None:
        nonlocal loop, stop, port
        loop, stop = asyncio.get_running_loop(), asyncio.Event()
        instance.loop = loop
        async with serve(
            handle,
            "127.0.0.1",
            0,
            ssl=context,
            open_timeout=0.5,
            process_request=handshake,
            close_timeout=0.5,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            ready.set()
            await stop.wait()

    def work() -> None:
        try:
            asyncio.run(run())
        except BaseException as error:
            failures.append(error)
        finally:
            ready.set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        assert ready.wait(2), "TLS provider did not start"
        assert not failures, failures
        monkeypatch.setattr(calculator, "WEBSOCKET_BASE", f"wss://localhost:{port}/")
        yield instance
    finally:
        if instance.timer is not None:
            instance.timer.cancel()
            instance.timer.join(timeout=1)
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=2)
        assert not thread.is_alive(), "TLS provider did not stop"
        assert not failures, failures


def test_secure_subscription_has_one_worker_ssl_owner(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real calculation must never share an SSL object across I/O threads."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    owners: dict[object, set[int]] = {}
    lock = threading.Lock()
    expected_owner: list[Optional[int]] = [None]

    def observe(method: Callable[..., Any]) -> Callable[..., Any]:
        def tracked(connection: Any, *args: Any, **kwargs: Any) -> Any:
            if not connection.server_side:
                current = threading.get_ident()
                with lock:
                    owners.setdefault(connection, set()).add(current)
                if current != expected_owner[0]:
                    raise RuntimeError("Client SSL accessed outside its worker owner")
            return method(connection, *args, **kwargs)

        return tracked

    for connection_type, methods in (
        (ssl.SSLSocket, ("do_handshake", "recv", "send")),
        (ssl.SSLObject, ("do_handshake", "read", "write", "unwrap")),
    ):
        for name in methods:
            monkeypatch.setattr(
                connection_type, name, observe(getattr(connection_type, name))
            )
    results: list[Any] = []
    failures: list[BaseException] = []

    def calculate() -> None:
        expected_owner[0] = threading.get_ident()
        try:
            results.append(
                calculator.JlcpcbCalculator(post=secure_provider.post).calculate_width(
                    *_inputs(), "F.Cu"
                )
            )
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(
        target=calculate, name="tls-calculator-owner", daemon=True
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "TLS calculation did not finish"
    assert owners, "The calculation did not exercise native client TLS"
    assert all(threads == {worker.ident} for threads in owners.values()), owners
    assert not failures, failures
    assert len(results) == 1 and results[0].status == "success", results
    assert secure_provider.closed.wait(1)


def test_secure_subscription_services_ping_and_early_result_before_http_ack(
    secure_provider: _SecureProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting HTTP acknowledgement must not stop full-duplex TLS processing."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    submitted, release = threading.Event(), threading.Event()
    results: list[Any] = []
    failures: list[BaseException] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = secure_provider.post(path, payload, **kwargs)
        if path.endswith("/calc"):
            submitted.set()
            assert release.wait(2), "The test did not release the HTTP acknowledgement"
        return response

    def calculate() -> None:
        try:
            results.append(
                calculator.JlcpcbCalculator(post=post).calculate_width(
                    *_inputs(), "F.Cu"
                )
            )
        except BaseException as error:
            failures.append(error)

    async def ping() -> None:
        subscription = secure_provider.connection
        assert isinstance(subscription, _SecureSubscription)
        pong = await subscription.connection.ping()
        await pong

    worker = threading.Thread(target=calculate, daemon=True)
    worker.start()
    try:
        assert submitted.wait(2), "HTTP submission did not start"
        asyncio.run_coroutine_threadsafe(ping(), secure_provider.loop).result(timeout=1)
        assert worker.is_alive(), "Calculation returned before the HTTP acknowledgement"
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive(), "TLS calculation did not finish"
    assert not failures, failures
    assert len(results) == 1 and results[0].status == "success", results
    assert results[0].target_width_nm == 215_900
    assert secure_provider.closed.wait(1)


@pytest.mark.parametrize("failure", (None, "unknown-ca", "wrong-hostname"))
def test_secure_subscription_uses_http_connect_proxy(
    secure_provider: _SecureProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Optional[str],
) -> None:
    """Verified proxy connections recover after bounded certificate failures."""
    original_endpoint = calculator.WEBSOCKET_BASE
    if failure == "wrong-hostname":
        monkeypatch.setattr(
            calculator,
            "WEBSOCKET_BASE",
            original_endpoint.replace("localhost", "127.0.0.1"),
        )
    endpoint = urlsplit(calculator.WEBSOCKET_BASE)
    expected = f"CONNECT {endpoint.hostname}:{endpoint.port} HTTP/1.1".encode()
    requests: list[bytes] = []
    failures: list[BaseException] = []
    tasks: set[asyncio.Task[Any]] = set()
    closed = threading.Event()

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(64 * 1024):
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()

    async def tunnel(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        upstream: Optional[asyncio.StreamWriter] = None
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            requests.append(headers.split(b"\r\n", 1)[0])
            assert requests[-1] == expected
            source, upstream = await asyncio.open_connection("127.0.0.1", endpoint.port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(
                asyncio.gather(pump(reader, upstream), pump(source, writer)), 2
            )
        except Exception as error:
            failures.append(error)
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
                if upstream is not None:
                    await asyncio.wait_for(upstream.wait_closed(), 1)
            except Exception as error:
                failures.append(error)
            tasks.discard(task)
            closed.set()

    async def start() -> asyncio.AbstractServer:
        return await asyncio.start_server(tunnel, "127.0.0.1", 0, limit=8192)

    proxy = asyncio.run_coroutine_threadsafe(start(), secure_provider.loop).result(
        timeout=1
    )
    port = proxy.sockets[0].getsockname()[1]
    monkeypatch.setenv("wss_proxy", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    if failure != "unknown-ca":
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))

    async def stop() -> None:
        proxy.close()
        await proxy.wait_closed()
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2)

    try:
        client = calculator.JlcpcbCalculator(post=secure_provider.post)
        started = time.monotonic()
        result = client.calculate_width(*_inputs(), "F.Cu")
        assert requests == [expected]
        if failure is not None:
            assert result.status == "unavailable", result
            assert "secure connection" in result.message
            assert result.target_width_nm is None and result.calculation_digest == ""
            assert secure_provider.submissions == []
            assert not secure_provider.connected.is_set()
            assert closed.wait(1), "Failed TLS proxy tunnel did not close"
            assert not tasks, "Failed TLS proxy left an active tunnel"
            assert time.monotonic() - started < 2.5
            failed_request = expected
            closed.clear()
            monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
            monkeypatch.setattr(calculator, "WEBSOCKET_BASE", original_endpoint)
            expected = f"CONNECT localhost:{endpoint.port} HTTP/1.1".encode()
            result = client.calculate_width(*_inputs(), "F.Cu")
            assert requests == [failed_request, expected]
        assert result.status == "success", result.message
        assert len(secure_provider.submissions) == 1
        assert secure_provider.closed.wait(1)
        assert closed.wait(1), "HTTP CONNECT tunnel did not close"
    finally:
        asyncio.run_coroutine_threadsafe(stop(), secure_provider.loop).result(timeout=3)
    assert not failures, failures
    assert not tasks
    for record in caplog.records:
        if record.levelno >= logging.ERROR:
            # websockets 15.0.1 calls connection_lost before connection_made
            # when start_tls rejects a CONNECT peer. Observe only that known
            # vendor cleanup diagnostic; unrelated errors must still fail.
            assert failure is not None and record.name == "asyncio"
            assert record.exc_info is not None
            assert isinstance(record.exc_info[1], AttributeError)
            assert str(record.exc_info[1]) == (
                "'ClientConnection' object has no attribute 'recv_messages'"
            )


@pytest.mark.parametrize("outcome", ("cancel", "deadline"))
def test_secure_subscription_abandoned_during_http_ack_closes_and_reopens(
    secure_provider: _SecureProvider,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """HTTP completion cannot apply a result after owner cancellation or expiry."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    now = [0.0]
    monkeypatch.setattr(calculator, "time", SimpleNamespace(monotonic=lambda: now[0]))
    submitted, release, cancel = threading.Event(), threading.Event(), threading.Event()
    results: list[Any] = []
    failures: list[BaseException] = []

    def post(path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = secure_provider.post(path, payload, **kwargs)
        if path.endswith("/calc") and not release.is_set():
            submitted.set()
            assert release.wait(2), "The test did not release the HTTP acknowledgement"
        return response

    client = calculator.JlcpcbCalculator(post=post)

    def calculate() -> None:
        try:
            results.append(client.calculate_width(*_inputs(), "F.Cu", cancel=cancel))
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=calculate, daemon=True)
    worker.start()
    try:
        assert submitted.wait(2), "HTTP submission did not start"
        if outcome == "cancel":
            cancel.set()
        else:
            now[0] = calculator.MAX_TOTAL_SECONDS + 1
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive(), "Abandoned calculation did not finish"
    if outcome == "cancel":
        assert len(failures) == 1 and isinstance(
            failures[0], calculator.CalculatorCancelled
        ), failures
        assert results == []
    else:
        assert not failures, failures
        assert len(results) == 1 and results[0].status == "unavailable", results
        assert "timed out" in results[0].message
    assert secure_provider.closed.wait(1)
    secure_provider.connected.clear()
    secure_provider.closed.clear()
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "success", result.message
    assert secure_provider.closed.wait(1)


@pytest.mark.parametrize("status", (300, 301, 302, 303, 307, 308))
def test_secure_subscription_rejects_redirect_without_contacting_destination(
    secure_provider: _SecureProvider,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An HTTP redirect must not move the fixed calculator subscription endpoint."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    contacted = threading.Event()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server-key.pem")

    async def handle(connection: ServerConnection) -> None:
        contacted.set()
        await connection.close()

    async def start() -> Any:
        return await serve(
            handle, "127.0.0.1", 0, ssl=context, open_timeout=0.5, close_timeout=0.5
        )

    destination = asyncio.run_coroutine_threadsafe(
        start(), secure_provider.loop
    ).result(timeout=1)
    port = destination.sockets[0].getsockname()[1]

    def redirect(connection: Any, request: Request) -> Response:
        response = connection.respond(status, "Calculator endpoint moved")
        response.headers["Location"] = f"wss://localhost:{port}/redirected"
        return response

    async def close() -> None:
        destination.close()
        await destination.wait_closed()

    monkeypatch.setattr(secure_provider, "handshake", redirect)
    try:
        result = calculator.JlcpcbCalculator(post=secure_provider.post).calculate_width(
            *_inputs(), "F.Cu"
        )
        assert result.status == "unavailable", result
        assert not contacted.is_set(), "The subscription followed an HTTP redirect"
        assert secure_provider.submissions == []
    finally:
        asyncio.run_coroutine_threadsafe(close(), secure_provider.loop).result(
            timeout=2
        )


@pytest.mark.parametrize("variable", ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"))
def test_private_ca_override_completes_strict_x509_secure_calculation(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """The public test chain must pass strict verification on older Python too."""
    monkeypatch.setenv(variable, str(FIXTURES / "ca.pem"))
    original_context = calculator._tls_context
    contexts: list[ssl.SSLContext] = []

    def strict_context() -> ssl.SSLContext:
        context = original_context()
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        contexts.append(context)
        return context

    monkeypatch.setattr(calculator, "_tls_context", strict_context)
    result = calculator.JlcpcbCalculator(post=secure_provider.post).calculate_width(
        *_inputs(), "F.Cu"
    )
    assert result.status == "success", result.message
    assert len(contexts) == 1
    assert contexts[0].verify_flags & ssl.VERIFY_X509_STRICT
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname
    assert len(secure_provider.submissions) == 1
    assert secure_provider.closed.wait(1)


@pytest.mark.parametrize("variable", ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"))
def test_private_ca_override_completes_real_secure_calculation(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """HTTP's private CA configuration must also authorize the solver subscription."""
    monkeypatch.setenv(variable, str(FIXTURES / "ca.pem"))
    result = calculator.JlcpcbCalculator(post=secure_provider.post).calculate_width(
        *_inputs(), "F.Cu"
    )
    assert result.status == "success", result.message
    assert secure_provider.closed.wait(1)


def test_requests_bundle_precedes_curl_bundle(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid first override wins even when the lower-priority one is unusable."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "missing.pem"))
    assert (
        calculator.JlcpcbCalculator(post=secure_provider.post)
        .calculate_width(*_inputs(), "F.Cu")
        .status
        == "success"
    )


def test_empty_requests_override_uses_curl_bundle(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty environment values have Requests' existing fall-through semantics."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
    monkeypatch.setenv("CURL_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    assert (
        calculator.JlcpcbCalculator(post=secure_provider.post)
        .calculate_width(*_inputs(), "F.Cu")
        .status
        == "success"
    )


def test_ca_directory_is_used_without_substitution(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A standard OpenSSL hashed CA directory is supported, not treated as a file."""
    shutil.copyfile(FIXTURES / "ca.pem", tmp_path / "7ff866ca.0")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path))
    assert (
        calculator.JlcpcbCalculator(post=secure_provider.post)
        .calculate_width(*_inputs(), "F.Cu")
        .status
        == "success"
    )


@pytest.mark.parametrize(
    "failure",
    (
        "missing",
        "malformed",
        "unreadable",
        "empty-directory",
        "unhashed-directory",
        "malformed-directory",
    ),
)
def test_bad_explicit_ca_fails_actionably_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """Explicit configuration errors cannot silently fall back to another trust store."""
    path = tmp_path / "ca.pem"
    if failure == "malformed":
        path.write_text("This is not a certificate", encoding="utf-8")
    elif failure == "unreadable":
        path.write_bytes((FIXTURES / "ca.pem").read_bytes())
        monkeypatch.setattr(calculator.os, "access", lambda *_args: False)
    elif failure.endswith("directory"):
        path.mkdir()
        if failure == "unhashed-directory":
            (path / "ca.pem").write_bytes((FIXTURES / "ca.pem").read_bytes())
        elif failure == "malformed-directory":
            (path / "7ff866ca.0").write_text("Not a certificate", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(path))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    connected = []

    def connect(*_args: Any, **_kwargs: Any) -> None:
        connected.append(True)
        raise AssertionError("Invalid explicit trust must fail before socket setup")

    client = calculator.JlcpcbCalculator(connector=connect)
    with pytest.raises(calculator.CalculatorUnavailable, match="REQUESTS_CA_BUNDLE"):
        client._solve("CoatedMicrostrip1B", {}, time.monotonic() + 1, None)
    assert connected == []


@pytest.mark.parametrize("failure", ("unknown-ca", "wrong-hostname"))
def test_secure_subscription_preserves_certificate_and_hostname_verification(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Custom trust cannot disable either certificate or hostname verification."""
    if failure == "wrong-hostname":
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
        monkeypatch.setattr(
            calculator,
            "WEBSOCKET_BASE",
            calculator.WEBSOCKET_BASE.replace("localhost", "127.0.0.1"),
        )
    result = calculator.JlcpcbCalculator(post=secure_provider.post).calculate_width(
        *_inputs(), "F.Cu"
    )
    assert result.status == "unavailable"
    assert "secure connection" in result.message
    assert secure_provider.submissions == []


def test_secure_subscription_cancels_and_closes_within_bound(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation releases a TLS subscription without producing a failed result."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    secure_provider.mode = "cancel"
    secure_provider.cancel = threading.Event()
    client = calculator.JlcpcbCalculator(post=secure_provider.post)
    with pytest.raises(calculator.CalculatorCancelled):
        client.calculate_width(*_inputs(), "F.Cu", cancel=secure_provider.cancel)
    assert time.monotonic() - secure_provider.cancelled_at < 2.5
    assert secure_provider.closed.wait(1)
    secure_provider.mode = "success"
    secure_provider.connected.clear()
    secure_provider.closed.clear()
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "success", result.message
    assert secure_provider.closed.wait(1)


@pytest.mark.parametrize("mode", ("invalid-json", "oversized"))
def test_secure_subscription_failure_closes_and_reopens(
    secure_provider: _Provider, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Protocol failures release the TLS session without poisoning the next solve."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    client = calculator.JlcpcbCalculator(post=secure_provider.post)
    secure_provider.mode = mode
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "unavailable", result
    assert result.target_width_nm is None and result.calculation_digest == ""
    assert secure_provider.closed.wait(1)
    secure_provider.mode = "success"
    secure_provider.connected.clear()
    secure_provider.closed.clear()
    result = client.calculate_width(*_inputs(), "F.Cu")
    assert result.status == "success", result.message
    assert secure_provider.closed.wait(1)


def test_no_overrides_keeps_default_verified_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal certifi/platform trust path remains verified."""
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    context = calculator._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_ca_override_loads_exact_private_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicitly configured private CA is loaded into the verified context."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(FIXTURES / "ca.pem"))
    context = calculator._tls_context()
    certificates = context.get_ca_certs()
    assert len(certificates) == 1
    assert certificates[0]["subject"] == (
        (("commonName", "Impedance loopback test CA"),),
    )
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
