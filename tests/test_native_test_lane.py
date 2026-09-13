"""Exercise required native lanes through isolated pytest processes."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional

import pytest

from .native_wx_support import run_native, wait_until


def run_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Optional[str],
    source: str,
    broken: str = "",
    collection_skip: bool = False,
    cli: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the real pytest hooks with isolated, deliberately limited bindings."""
    hooks = Path(__file__).parents[1] / "conftest.py"
    shutil.copyfile(hooks, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    native_wx\n    native_kicad\n    os_input\n"
    )
    (tmp_path / "wx").mkdir()
    (tmp_path / "wx" / "__init__.py").write_text(
        "raise ImportError('missing wx binding')\n"
        if broken == "wx"
        else "def version(): return '4.2.1 gtk3 (wxWidgets 3.2.4)'\n"
    )
    (tmp_path / "wx" / "grid.py").write_text("class Grid: pass\n")
    (tmp_path / "pcbnew.py").write_text(
        "raise ImportError('missing pcbnew binding')\n"
        if broken == "pcbnew"
        else "class BOARD:\n"
        + (
            "    pass\n"
            if broken == "variants"
            else "    def GetVariantNamesForUI(self): return ['', 'A']\n"
            "    def AddVariant(self, name): pass\n"
        )
        + "def GetBuildVersion(): return '10.0.6'\n"
    )
    (tmp_path / "test_probe.py").write_text("import pytest\n" + source)
    if collection_skip:
        (tmp_path / "test_skipped.py").write_text(
            "import pytest\npytest.skip('missing at collection', allow_module_level=True)\n"
        )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("KICAD_JLCPCB_NATIVE_TESTS", raising=False)
    monkeypatch.delenv("KICAD_JLCPCB_REQUIRE_NATIVE", raising=False)
    if mode and not cli:
        monkeypatch.setenv("KICAD_JLCPCB_REQUIRE_NATIVE", mode)
    command = [sys.executable, "-m", "pytest", "-q"]
    if cli:
        command.append(f"--require-native={mode}")
    return subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize("broken", ["wx", "pcbnew", "variants"])
def test_required_runtime_fails_before_collecting_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """An absent or old runtime cannot turn the required lane into green skips."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "raise AssertionError('collection happened before runtime validation')\n",
        broken,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "Required native kicad runtime unavailable" in result.stderr


@pytest.mark.parametrize(
    "mode, marker", [("wx", "native_wx"), ("kicad", "native_kicad")]
)
def test_required_native_fixture_skip_fails_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, marker: str
) -> None:
    """A later fixture skip fails even when the runtime preflight succeeded."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        mode,
        "@pytest.fixture\n"
        "def runtime(): pytest.skip('binding unavailable after collection')\n"
        f"@pytest.mark.{marker}\n"
        "def test_native(runtime): pass\n",
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, (
        result.stdout + result.stderr
    )
    assert "Required native test skipped" in result.stdout
    assert "binding unavailable after collection" in result.stdout


def test_required_lane_rejects_no_native_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting only ordinary tests is not evidence that native checks passed."""
    result = run_lane(tmp_path, monkeypatch, "kicad", "def test_ordinary(): pass\n")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "No native_kicad tests selected" in result.stderr


def test_required_lane_rejects_partial_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One runnable test must not hide a native module skipped during collection."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\ndef test_native(): pass\n",
        collection_skip=True,
    )
    assert result.returncode == pytest.ExitCode.INTERRUPTED, (
        result.stdout + result.stderr
    )
    assert "Required native collection skipped" in result.stdout


def test_cli_option_enables_native_tests_and_propagates_to_child_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI's CLI option opts in and carries strictness to input-test subprocesses."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "wx",
        "import os\n@pytest.mark.native_wx\n"
        "def test_native():\n"
        "    assert os.environ['KICAD_JLCPCB_REQUIRE_NATIVE'] == 'wx'\n"
        "    assert os.environ['KICAD_JLCPCB_NATIVE_TESTS'] == '1'\n",
        cli=True,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr


@pytest.mark.parametrize("mode", [None, "wx", "kicad"])
def test_optional_skips_and_successful_native_tests_remain_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Optional[str]
) -> None:
    """Local skips remain optional; required runs tolerate unrelated skips."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        mode,
        "@pytest.mark.native_wx\n@pytest.mark.native_kicad\n"
        "def test_native(): pass\n"
        "def test_optional(): pytest.skip('optional dependency')\n",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("failure", ["exercise", "after_events", "predicate"])
def test_native_runner_releases_windows_and_restores_existing_app(failure: str) -> None:
    """A failed callback cannot leave native children for a later test's GC."""
    import wx

    app = wx.App(False)
    previous = wx.Frame(None, title="Existing host")
    app.SetTopWindow(previous)
    created = []

    def fail(_frame: Any, _wx: Any) -> None:
        if failure == "predicate":
            wait_until(wx, lambda: False, timeout_ms=1, reason="expected failure")
        raise AssertionError("expected failure")

    def exercise(frame: Any, wx: Any) -> None:
        created.extend((frame, wx.Dialog(frame, title="Owned child")))
        if failure == "exercise":
            fail(frame, wx)

    try:
        with pytest.raises(AssertionError, match="expected failure"):
            run_native(exercise, after_events=None if failure == "exercise" else fail)
        assert all(not window for window in created)
        assert wx.GetApp() is app and app.GetTopWindow() is previous
        assert previous.IsEnabled()
        assert set(wx.GetTopLevelWindows()) == {previous}
    finally:
        try:
            for window in [previous, *created]:
                if window:
                    window.Destroy()
            wait_until(wx, lambda: all(not window for window in [previous, *created]))
        finally:
            app.Destroy()
