"""Tests for the kipy-backed KiCad IPC transport client."""

from unittest.mock import patch

import pytest

from ipc_client import KiCadIPCClient, KiCadIPCError


def test_default_socket_path_uses_env_override(monkeypatch):
    """Environment override should win over platform defaults."""
    monkeypatch.setenv("KICAD_IPC_SOCKET", "/tmp/kicad-ipc-test.sock")

    assert KiCadIPCClient.default_socket_path() == "/tmp/kicad-ipc-test.sock"


def test_default_socket_path_prefers_kicad_api_socket(monkeypatch):
    """KiCad IPC launcher variable should take precedence when present."""
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad-api.sock")
    monkeypatch.setenv("KICAD_IPC_SOCKET", "/tmp/kicad-ipc-test.sock")

    assert KiCadIPCClient.default_socket_path() == "/tmp/kicad-api.sock"


def test_normalize_socket_path_accepts_ipc_uri():
    """`ipc://` socket endpoints should be preserved as-is."""
    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")
    assert client.socket_path == "ipc:///tmp/kicad/api.sock"


def test_normalize_socket_path_converts_plain_filesystem_path():
    """Plain Unix socket paths should be promoted to `ipc://` endpoints."""
    client = KiCadIPCClient(socket_path="/tmp/kicad/api.sock")
    assert client.socket_path == "ipc:///tmp/kicad/api.sock"


def test_is_available_true_when_ping_succeeds():
    """Availability should be true when kipy ping returns without error."""
    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")
    fake_kicad = type("FakeKiCad", (), {"ping": lambda self: None})()

    with patch.object(client, "_kicad_client", return_value=fake_kicad):
        assert client.is_available() is True


def test_is_available_false_when_ping_raises():
    """Availability should be false when kipy raises a connection error."""
    from kipy.errors import ConnectionError as KiPyConnectionError

    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")

    def _raise_conn(*_a, **_kw):
        raise KiPyConnectionError("refused")

    fake_kicad = type("FakeKiCad", (), {"ping": _raise_conn})()

    with patch.object(client, "_kicad_client", return_value=fake_kicad):
        assert client.is_available() is False


def test_board_returns_kipy_board():
    """board() should delegate to the kipy client's get_board()."""
    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")
    fake_board = object()
    fake_kicad = type("FakeKiCad", (), {"get_board": lambda self: fake_board})()

    with patch.object(client, "_kicad_client", return_value=fake_kicad):
        assert client.board() is fake_board


def test_export_gerbers_raises():
    """export_gerbers should raise KiCadIPCError until mapped."""
    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")
    with pytest.raises(KiCadIPCError):
        client.export_gerbers("/tmp/test.kicad_pcb", "/tmp/out")


def test_export_drill_raises():
    """export_drill should raise KiCadIPCError until mapped."""
    client = KiCadIPCClient(socket_path="ipc:///tmp/kicad/api.sock")
    with pytest.raises(KiCadIPCError):
        client.export_drill("/tmp/test.kicad_pcb", "/tmp/out")
