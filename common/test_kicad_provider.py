"""Tests for provider-side KiCad backend selection."""

import kicad_api
from kicad_api import (
    IPC_MINIMUM_VERSION,
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


def test_provider_uses_swig_below_ipc_minimum(monkeypatch):
    """Older KiCad versions should stay on SWIG adapters."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.delenv("KICAD_API_TOKEN", raising=False)
    monkeypatch.delenv("KICAD_IPC_SOCKET", raising=False)
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.0.1")

    adapters = KicadProvider.create_adapter_set()

    assert isinstance(adapters.board, SWIGBoardAdapter)
    assert isinstance(adapters.footprint, SWIGFootprintAdapter)
    assert isinstance(adapters.utility, SWIGUtilityAdapter)
    assert adapters.version < IPC_MINIMUM_VERSION


def test_provider_uses_swig_without_ipc_launch_context(monkeypatch):
    """Supported versions should remain on SWIG if IPC launch context is absent."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.delenv("KICAD_API_TOKEN", raising=False)
    monkeypatch.delenv("KICAD_IPC_SOCKET", raising=False)
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")

    adapters = KicadProvider.create_adapter_set()

    assert isinstance(adapters.board, SWIGBoardAdapter)
    assert isinstance(adapters.footprint, SWIGFootprintAdapter)
    assert isinstance(adapters.utility, SWIGUtilityAdapter)


def test_provider_uses_ipc_in_ipc_launch_context(monkeypatch):
    """Supported versions should use IPC when KiCad IPC launch context is present."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")
    monkeypatch.setattr(kicad_api, "_get_ipc_client_class", lambda: _FakeIPCClient)
    monkeypatch.setattr(
        kicad_api,
        "_get_ipc_adapter_classes",
        lambda: (_FakeIPCBoardAdapter, _FakeIPCFootprintAdapter, _FakeIPCUtilityAdapter),
    )

    adapters = KicadProvider.create_adapter_set()

    assert isinstance(adapters.board, _FakeIPCBoardAdapter)
    assert isinstance(adapters.footprint, _FakeIPCFootprintAdapter)
    assert isinstance(adapters.utility, _FakeIPCUtilityAdapter)
    assert isinstance(adapters.board.client, _FakeIPCClient)


def test_provider_falls_back_to_swig_when_ipc_unavailable(monkeypatch):
    """Supported versions should fall back to SWIG when the IPC server is unavailable."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")
    monkeypatch.setattr(kicad_api, "kicad_pcbnew", _FakePcbnew())
    monkeypatch.setattr(kicad_api, "_get_kicad_version", lambda: "8.99.0")
    monkeypatch.setattr(
        kicad_api,
        "_get_ipc_client_class",
        lambda: (lambda: _FakeIPCClient(available=False)),
    )
    monkeypatch.setattr(
        kicad_api,
        "_get_ipc_adapter_classes",
        lambda: (_FakeIPCBoardAdapter, _FakeIPCFootprintAdapter, _FakeIPCUtilityAdapter),
    )

    adapters = KicadProvider.create_adapter_set()

    assert isinstance(adapters.board, SWIGBoardAdapter)
    assert isinstance(adapters.footprint, SWIGFootprintAdapter)
    assert isinstance(adapters.utility, SWIGUtilityAdapter)