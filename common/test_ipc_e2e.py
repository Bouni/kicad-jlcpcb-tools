"""Integration-style bootstrap tests for IPC plugin startup."""

import importlib


class _FakeIPCClient:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path

    def is_available(self):
        return True


class _FakeIPCClientUnavailable:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path

    def is_available(self):
        return False


class _FakeProvider:
    calls = []

    def create_adapter_set(self, **kwargs):
        self.calls.append(kwargs)
        return {"adapter_set": "ok"}


class _FakeWindow:
    created_with = None
    centered = False
    shown = False

    def __init__(self, parent, adapter_set=None):
        self.__class__.created_with = {
            "parent": parent,
            "adapter_set": adapter_set,
        }

    def Center(self):
        self.__class__.centered = True

    def Show(self):
        self.__class__.shown = True


def _fresh_module():
    module = importlib.import_module("ipc_plugin_main")
    return importlib.reload(module)


def test_ipc_plugin_main_errors_without_socket(monkeypatch):
    """Main returns non-zero when IPC socket env var is missing."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)

    ipc_plugin_main = _fresh_module()

    assert ipc_plugin_main.main() == 1


def test_ipc_plugin_main_errors_when_ipc_unavailable(monkeypatch):
    """Main returns non-zero when IPC transport is unavailable."""
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc.sock")

    ipc_plugin_main = _fresh_module()
    monkeypatch.setattr(ipc_plugin_main, "KiCadIPCClient", _FakeIPCClientUnavailable)

    assert ipc_plugin_main.main() == 1


def test_ipc_plugin_main_bootstraps_successfully(monkeypatch):
    """Main initializes IPC client, provider, and UI window in order."""
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc.sock")

    ipc_plugin_main = _fresh_module()

    fake_provider = _FakeProvider()
    monkeypatch.setattr(ipc_plugin_main, "KiCadIPCClient", _FakeIPCClient)
    monkeypatch.setattr(ipc_plugin_main, "KicadProvider", lambda: fake_provider)
    # Patch _import_mainwindow so we don't try to import wx/KiCad in tests
    monkeypatch.setattr(ipc_plugin_main, "_import_mainwindow", lambda _dir: _FakeWindow)

    _FakeProvider.calls = []
    _FakeWindow.created_with = None
    _FakeWindow.centered = False
    _FakeWindow.shown = False

    assert ipc_plugin_main.main() == 0
    assert len(_FakeProvider.calls) == 1
    assert _FakeProvider.calls[0]["launch_context"] == "ipc"
    assert isinstance(_FakeProvider.calls[0]["ipc_client"], _FakeIPCClient)
    assert _FakeWindow.created_with is not None
    assert _FakeWindow.created_with["adapter_set"] == {"adapter_set": "ok"}
    assert _FakeWindow.centered is True
    assert _FakeWindow.shown is True
