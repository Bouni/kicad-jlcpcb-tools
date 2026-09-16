"""Exercise package registration and launch at the supported KiCad boundary."""

import importlib.util
from pathlib import Path
import sys
from typing import Any
from unittest.mock import ANY, MagicMock

import pytest

from tests.test_plugin_board_actions import Editor
from tests.wx_harness import module, temporary_modules, wx_stubs


@pytest.mark.parametrize(
    "version, supported",
    [
        ("6.0.11", False),
        ("6.99.0", False),
        ("unknown", False),
        ("7.0.0", True),
        ("7.0.2-2.fc42", True),
        ("8.0.9", True),
        ("9.0.6", True),
        ("10.0.6", True),
        ("10.99.0-1234-gabcdef-dirty", True),
    ],
)
def test_package_launch_checks_version_before_loading_window(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    """Manual installs reject old hosts without importing project-facing code."""
    package = "_plugin_version_test"
    root = Path(__file__).resolve().parents[1]
    registered = []
    window_imports = []
    window = MagicMock()
    editor = Editor()

    class ActionPlugin:
        def __init__(self) -> None:
            self.defaults()

        def defaults(self) -> None:
            """Allow the real plugin's defaults override to run."""

        def register(self) -> None:
            """Remember the instance KiCad would expose as an action."""
            registered.append(self)

    def window_attribute(name: str) -> Any:
        if name != "JLCPCBTools":
            raise AttributeError(name)
        assert supported, "Unsupported hosts must not import the main window"
        window_imports.append(name)
        return window

    spec = importlib.util.spec_from_file_location(package, root / "__init__.py")
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    wx = wx_stubs(
        submodules=(), MessageBox=MagicMock(), GetTopLevelWindows=lambda: [editor]
    )
    replacements = {
        package: entry,
        f"{package}.mainwindow": module(
            f"{package}.mainwindow", __getattr__=window_attribute
        ),
        "pcbnew": module(
            "pcbnew",
            ActionPlugin=ActionPlugin,
            GetBuildVersion=lambda: version,
            GetBoard=lambda: editor.board,
        ),
        **wx,
    }
    monkeypatch.setattr(sys, "path", list(sys.path))
    with temporary_modules(replacements, namespaces=(package,)):
        spec.loader.exec_module(entry)
        assert len(registered) == 1
        assert registered[0].name == "JLCPCB Tools"
        assert window_imports == []
        for _ in range(2):
            registered[0].Run()
        if supported:
            assert window.call_count == 2
            window.assert_called_with(None, board_action=ANY)
            assert callable(window.call_args.kwargs["board_action"])
            assert window.return_value.Center.call_count == 2
            assert window.return_value.Show.call_count == 2
            wx["wx"].MessageBox.assert_not_called()
        else:
            assert window_imports == []
            window.assert_not_called()
            assert wx["wx"].MessageBox.call_count == 2
            assert "KiCad 7.0 or newer" in wx["wx"].MessageBox.call_args.args[0]
