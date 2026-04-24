"""Tests for IPC initialization and provider launch-context routing."""

import pytest

from ipc_client import KiCadIPCClient
import kicad_api
from kicad_api import (
    KicadProvider,
    SWIGBoardAdapter,
    SWIGFootprintAdapter,
    SWIGUtilityAdapter,
)


class _FakePcbnew:
    pass


class _FakeIPCClient:
    def __init__(self, available=True):
        self._available = available

    def is_available(self):
        return self._available


class _FakeIPCBoardAdapter:
    def __init__(self, client):
        self.client = client


class _FakeIPCFootprintAdapter:
    def __init__(self, client):
        self.client = client


class _FakeIPCUtilityAdapter:
    def __init__(self, client):
        self.client = client


def test_ipc_client_reads_token_from_environment(monkeypatch):
    """Client should pull token from KICAD_API_TOKEN when not provided."""
    monkeypatch.setenv("KICAD_API_TOKEN", "token-from-env")

    client = KiCadIPCClient(socket_path="socket-does-not-exist")

    assert client.token == "token-from-env"


def test_ipc_client_explicit_token_overrides_environment(monkeypatch):
    """Explicit token should override environment token."""
    monkeypatch.setenv("KICAD_API_TOKEN", "token-from-env")

    client = KiCadIPCClient(socket_path="socket-does-not-exist", token="token-explicit")

    assert client.token == "token-explicit"


def test_provider_uses_injected_ipc_client_for_ipc_launch_context(monkeypatch):
    """Provider should use injected IPC client when launch_context='ipc'."""
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")
    monkeypatch.setattr(
        kicad_api,
        "_get_ipc_adapter_classes",
        lambda: (
            _FakeIPCBoardAdapter,
            _FakeIPCFootprintAdapter,
            _FakeIPCUtilityAdapter,
        ),
    )

    injected_client = _FakeIPCClient(available=True)
    adapters = KicadProvider.create_adapter_set(
        launch_context="ipc",
        ipc_client=injected_client,
    )

    assert isinstance(adapters.board, _FakeIPCBoardAdapter)
    assert isinstance(adapters.footprint, _FakeIPCFootprintAdapter)
    assert isinstance(adapters.utility, _FakeIPCUtilityAdapter)
    assert adapters.board.client is injected_client


def test_provider_ipc_launch_context_falls_back_when_injected_client_unavailable(
    monkeypatch,
):
    """Provider should fall back to SWIG when injected IPC client is unavailable."""
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")
    monkeypatch.setattr(
        kicad_api,
        "_get_ipc_adapter_classes",
        lambda: (
            _FakeIPCBoardAdapter,
            _FakeIPCFootprintAdapter,
            _FakeIPCUtilityAdapter,
        ),
    )

    adapters = KicadProvider.create_adapter_set(
        launch_context="ipc",
        ipc_client=_FakeIPCClient(available=False),
    )

    assert isinstance(adapters.board, SWIGBoardAdapter)
    assert isinstance(adapters.footprint, SWIGFootprintAdapter)
    assert isinstance(adapters.utility, SWIGUtilityAdapter)


def test_provider_swig_launch_context_forces_swig(monkeypatch):
    """launch_context='swig' should force SWIG even when IPC env is present."""
    monkeypatch.setenv("KICAD_API_SOCKET", "kicad-api.sock")
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")

    adapters = KicadProvider.create_adapter_set(launch_context="swig")

    assert isinstance(adapters.board, SWIGBoardAdapter)
    assert isinstance(adapters.footprint, SWIGFootprintAdapter)
    assert isinstance(adapters.utility, SWIGUtilityAdapter)


def test_provider_rejects_unknown_launch_context(monkeypatch):
    """Unknown launch_context value should raise ValueError."""
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())

    with pytest.raises(ValueError, match="launch_context"):
        KicadProvider.create_adapter_set(launch_context="unknown")
