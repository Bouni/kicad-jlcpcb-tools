"""Exercise required native lanes through isolated pytest processes."""

import gc
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Optional
import weakref

import pytest

from .native_wx_support import (
    create_native_app,
    isolated_native,
    pump,
    run_native,
    wait_until,
)


def run_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Optional[str],
    source: str,
    broken: str = "",
    collection_skip: bool = False,
    cli: bool = False,
    version: str = "10.0.6",
    kicad_major: Optional[str] = None,  # noqa: UP045
    golden_update: str = "",
    parallel: bool = False,
    real_wx: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the real pytest hooks with isolated, deliberately limited bindings.

    ``broken="variants"`` gives a KiCad without the design-variant API, as
    KiCad 9 is; ``kicad_major`` is the lane's ``--kicad-major``, and
    ``golden_update`` its ``KICAD_JLCPCB_GOLDEN_UPDATE``, which the child
    otherwise runs without.
    """
    hooks = Path(__file__).parents[1] / "conftest.py"
    shutil.copyfile(hooks, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    native_wx\n    native_kicad\n    os_input\n"
        "    kicad_variants\n    kicad_standalone_drc\n    kicad_golden\n"
    )
    if not real_wx:
        (tmp_path / "wx").mkdir()
        (tmp_path / "wx" / "__init__.py").write_text(
            "raise ImportError('missing wx binding')\n"
            if broken == "wx"
            else "def version(): return '4.2.1 gtk3 (wxWidgets 3.2.4)'\n"
        )
        (tmp_path / "wx" / "grid.py").write_text("class Grid: pass\n")
    (tmp_path / "pcbnew.py").write_text(
        "raise ModuleNotFoundError('missing pcbnew binding')\n"
        if broken == "pcbnew"
        else "class BOARD:\n"
        + (
            "    pass\n"
            if broken == "variants"
            else "    def GetVariantNamesForUI(self): return ['', 'A']\n"
            "    def AddVariant(self, name): pass\n"
        )
        + f"def GetBuildVersion(): return {version!r}\n"
    )
    (tmp_path / "test_probe.py").write_text("import pytest\n" + source)
    if parallel:
        (tmp_path / "test_other.py").write_text("import pytest\n" + source)
    if collection_skip:
        (tmp_path / "test_skipped.py").write_text(
            "import pytest\npytest.skip('missing at collection', allow_module_level=True)\n"
        )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("KICAD_JLCPCB_NATIVE_TESTS", raising=False)
    monkeypatch.delenv("KICAD_JLCPCB_REQUIRE_NATIVE", raising=False)
    monkeypatch.delenv("KICAD_JLCPCB_KICAD_MAJOR", raising=False)
    monkeypatch.delenv("KICAD_JLCPCB_GOLDEN_UPDATE", raising=False)
    if golden_update:
        monkeypatch.setenv("KICAD_JLCPCB_GOLDEN_UPDATE", golden_update)
    if mode and not cli:
        monkeypatch.setenv("KICAD_JLCPCB_REQUIRE_NATIVE", mode)
    command = [sys.executable, "-m", "pytest", "-q", "-rs"]
    if cli:
        command.append(f"--require-native={mode}")
    if kicad_major:
        command.append(f"--kicad-major={kicad_major}")
    if parallel:
        command += [
            "-p",
            "xdist.plugin",
            "-n",
            "2",
            "--dist=loadfile",
            "--max-worker-restart=0",
        ]
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


@pytest.mark.parametrize(
    "version, kicad_major, expected",
    [
        ("10.0.6", "9", "expected KiCad 9"),
        ("9.0.9", None, "expected KiCad 10"),
        ("8.0.9-8.0.9-0~ubuntu24.04.1", "7", "expected KiCad 7"),
        ("7.0.11+dfsg-1build4", "8", "expected KiCad 8"),
    ],
)
def test_kicad_lane_requires_its_own_major_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    kicad_major: Optional[str],  # noqa: UP045
    expected: str,
) -> None:
    """Each KiCad lane fails before collection when another KiCad is installed."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "raise AssertionError('collection happened before runtime validation')\n",
        "" if version.startswith("10.") else "variants",
        version=version,
        kicad_major=kicad_major,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert expected in result.stderr


@pytest.mark.parametrize(
    "version, kicad_major",
    [
        ("9.0.9-9.0.9~ubuntu24.04.1", "9"),
        ("8.0.9-8.0.9-0~ubuntu24.04.1", "8"),
        ("7.0.11+dfsg-1build4", "7"),
    ],
)
def test_a_lane_without_variants_skips_only_the_variant_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, kicad_major: str
) -> None:
    """Without the variant API a variant test skips with its reason; others run.

    The versions are the builds the KiCad 9, 8 and 7 lanes install.
    """
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "import os\n"
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_variants\n"
        "def test_variants(): raise AssertionError('variant test ran')\n"
        "@pytest.mark.native_kicad\n"
        "def test_board():\n"
        f"    assert os.environ['KICAD_JLCPCB_KICAD_MAJOR'] == {kicad_major!r}\n",
        "variants",
        version=version,
        kicad_major=kicad_major,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
    assert (
        "KiCad has no design variants (BOARD.GetVariantNamesForUI is unavailable)"
        in result.stdout
    )


def test_a_kicad9_lane_of_only_variant_tests_runs_nothing_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Variant tests skipped for the missing API are not native tests that ran."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_variants\n"
        "def test_variants(): raise AssertionError('variant test ran')\n",
        "variants",
        version="9.0.9",
        kicad_major="9",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "No native_kicad tests selected that would run" in result.stderr


def test_an_isolated_rerun_in_the_kicad9_lane_accepts_kicad9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test rerun in a fresh process, as isolated native tests are, keeps the lane."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "import os, subprocess, sys\n"
        "@pytest.mark.native_kicad\n"
        "def test_board(request):\n"
        "    if os.environ.get('LANE_CHILD') == '1':\n"
        "        return\n"
        "    child = subprocess.run(\n"
        "        [sys.executable, '-m', 'pytest', '-q', request.node.nodeid],\n"
        "        env=dict(\n"
        "            os.environ, LANE_CHILD='1', KICAD_JLCPCB_REQUIRE_NATIVE='kicad'\n"
        "        ),\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=False,\n"
        "    )\n"
        "    assert child.returncode == 0, child.stdout + child.stderr\n",
        "variants",
        version="9.0.9",
        kicad_major="9",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("broken", ["variants", "pcbnew"])
def test_an_optional_run_isolates_a_kicad_test_on_the_kicad_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """Outside a lane, an isolated KiCad test runs on KiCad 9, or skips without KiCad."""
    support = Path(__file__).with_name("native_wx_support.py")
    shutil.copyfile(support, tmp_path / "native_wx_support.py")
    result = run_lane(
        tmp_path,
        monkeypatch,
        None,
        "from native_wx_support import isolated_native\n"
        "@pytest.mark.native_kicad\n"
        "def test_board(request):\n"
        "    if isolated_native(request, 'LANE_CHILD', native='kicad'):\n"
        "        return\n"
        "    import pcbnew\n"
        "    assert pcbnew.GetBuildVersion() == '9.0.9'\n",
        broken,
        version="9.0.9",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    if broken == "pcbnew":
        assert "1 skipped" in result.stdout
        assert "KiCad pcbnew is unavailable" in result.stdout
    else:
        assert "1 passed" in result.stdout


def test_variant_tests_collect_where_kicad_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without pcbnew the capability check leaves variant tests to their fixtures."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        None,
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_variants\n"
        "def test_variants(): pytest.skip('KiCad pcbnew is unavailable')\n",
        "pcbnew",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "KiCad pcbnew is unavailable" in result.stdout


@pytest.mark.parametrize(
    "version, kicad_major, broken",
    [("9.0.9", "9", "variants"), ("10.0.6", None, "")],
)
def test_a_variant_test_skipped_for_another_reason_fails_the_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    kicad_major: Optional[str],  # noqa: UP045
    broken: str,
) -> None:
    """Only the missing variant API excuses a skip, and only where it is missing."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n"
        "def test_board(): pytest.skip('board fixture unavailable')\n"
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_variants\n"
        "def test_variants(): pytest.skip('variant fixture unavailable')\n",
        broken,
        version=version,
        kicad_major=kicad_major,
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, (
        result.stdout + result.stderr
    )
    assert "Required native test skipped" in result.stdout
    assert "board fixture unavailable" in result.stdout
    if broken:
        assert "1 failed, 1 skipped" in result.stdout
    else:
        assert "2 failed" in result.stdout
        assert "variant fixture unavailable" in result.stdout


def test_kicad10_lane_skips_only_the_standalone_drc_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KiCad 10's standalone DRC crashes, so its test skips and names the defect."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_standalone_drc\n"
        "def test_drc(): raise AssertionError('standalone DRC ran on KiCad 10')\n"
        "@pytest.mark.native_kicad\n"
        "def test_board(): pass\n",
        version="10.0.7-10.0.7~ubuntu24.04.1",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
    assert "standalone WriteDRCReport segfaults" in result.stdout
    assert "dereferences a null Pgm()" in result.stdout
    assert "KiCad issue #25755" in result.stdout


@pytest.mark.parametrize(
    "version, kicad_major",
    [
        ("9.0.9-9.0.9~ubuntu24.04.1", "9"),
        ("8.0.9-8.0.9-0~ubuntu24.04.1", "8"),
        ("7.0.11+dfsg-1build4", "7"),
    ],
)
def test_the_standalone_drc_tests_run_before_kicad_10(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, kicad_major: str
) -> None:
    """The DRC skip is keyed to KiCad 10 alone; earlier lanes must run the test."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_standalone_drc\n"
        "def test_drc(): pass\n",
        "variants",
        version=version,
        kicad_major=kicad_major,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout and "skipped" not in result.stdout


def test_a_kicad10_lane_of_only_standalone_drc_tests_runs_nothing_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipped DRC tests are not native tests that ran."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_standalone_drc\n"
        "def test_drc(): pass\n",
        version="10.0.7-10.0.7~ubuntu24.04.1",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "No native_kicad tests selected that would run" in result.stderr


def test_a_standalone_drc_test_skipped_for_another_reason_fails_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before KiCad 10 the DRC marker excuses no skip at all."""
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_standalone_drc\n"
        "def test_drc(): pytest.skip('report unavailable')\n",
        "variants",
        version="9.0.9",
        kicad_major="9",
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, (
        result.stdout + result.stderr
    )
    assert "Required native test skipped" in result.stdout
    assert "report unavailable" in result.stdout


def _record_golden_outputs(tmp_path: Path, *majors: str) -> None:
    """Record golden outputs for these KiCad majors, as kicad_builds.json does."""
    golden = tmp_path / "tests" / "golden"
    golden.mkdir(parents=True)
    (golden / "kicad_builds.json").write_text(
        json.dumps({major: f"{major}.0.9" for major in majors}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "version, kicad_major",
    [("8.0.9-8.0.9-0~ubuntu24.04.1", "8"), ("7.0.11+dfsg-1build4", "7")],
)
def test_golden_tests_skip_on_a_kicad_with_no_recorded_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, kicad_major: str
) -> None:
    """A KiCad with no recorded outputs skips its golden tests."""
    _record_golden_outputs(tmp_path, "9", "10")
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_golden\n"
        "def test_golden(): raise AssertionError('golden test ran')\n"
        "@pytest.mark.native_kicad\n"
        "def test_board(): pass\n",
        "variants",
        version=version,
        kicad_major=kicad_major,
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
    assert f"No golden outputs are recorded for KiCad {kicad_major}" in result.stdout


def test_golden_tests_run_where_outputs_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record decides, so a recorded KiCad runs its golden tests."""
    _record_golden_outputs(tmp_path, "9", "10")
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_golden\n"
        "def test_golden(): pass\n",
        "variants",
        version="9.0.9-9.0.9~ubuntu24.04.1",
        kicad_major="9",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout and "skipped" not in result.stdout


def test_update_mode_runs_golden_tests_to_record_a_new_major(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a major's first outputs needs its golden tests to run."""
    _record_golden_outputs(tmp_path, "9", "10")
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_golden\n"
        "def test_golden(): pass\n",
        "variants",
        version="7.0.11+dfsg-1build4",
        kicad_major="7",
        golden_update="1",
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "1 passed" in result.stdout and "skipped" not in result.stdout


def test_a_lane_of_only_unrecorded_golden_tests_runs_nothing_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipped golden tests are not native tests that ran."""
    _record_golden_outputs(tmp_path, "9", "10")
    result = run_lane(
        tmp_path,
        monkeypatch,
        "kicad",
        "@pytest.mark.native_kicad\n@pytest.mark.kicad_golden\n"
        "def test_golden(): pass\n",
        "variants",
        version="7.0.11+dfsg-1build4",
        kicad_major="7",
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "No native_kicad tests selected that would run" in result.stderr


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

    app = create_native_app(wx)
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


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("dispatch", ["modal", "pump", "wait"])
@pytest.mark.parametrize("depth", [1, 3])
def test_native_watchdog_unwinds_nested_dispatch_and_allows_reuse(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    dispatch: str,
    depth: int,
) -> None:
    """Expiry must unwind inner loops once, preserve the host, and stop interaction."""
    if isolated_native(request, "KICAD_NATIVE_WATCHDOG_CHILD"):
        return
    import wx

    app = wx.App(False)
    host = wx.Frame(None, title="Existing host")
    app.SetTopWindow(host)
    endings, phases, created = [], [], []
    watchdog, armed = [], []
    call_later = wx.CallLater

    def schedule(delay: int, callback: Any, *args: Any, **kwargs: Any) -> Any:
        # Exercise the baseline's real watchdog too: do not require a new API.
        timer = call_later(4500 if delay == 20000 else delay, callback, *args, **kwargs)
        if delay == 20000:
            watchdog.append(timer)
        return timer

    def arm(dialog: Any) -> None:
        assert dialog.IsModal() and watchdog
        armed.append(True)
        watchdog[-1].Start(1)

    class Dialog(wx.Dialog):
        def EndModal(self, code: int) -> None:
            endings.append(self.GetTitle())
            super().EndModal(code)

    class ExistingHook(wx.ModalDialogHook):
        def Enter(self, dialog: Any) -> int:
            return wx.ID_CANCEL if dialog.GetTitle() == "Blocked" else wx.ID_NONE

        def Exit(self, _dialog: Any) -> None:
            pass

    hook = ExistingHook()
    hook.Register()
    monkeypatch.setattr(wx, "CallLater", schedule)

    def exercise(frame: Any, _wx: Any) -> None:
        created.append(frame)

        def show(level: int, parent: Any) -> None:
            dialog = Dialog(parent, title=str(level))
            created.append(dialog)

            def interact() -> None:
                if level + 1 < depth:
                    wx.CallAfter(show, level + 1, dialog)
                else:
                    dialog.SetSize((420, 180))
                    wx.CallAfter(arm, dialog)
                if level or dispatch == "modal":
                    return
                try:
                    if dispatch == "wait":
                        wait_until(wx, lambda: False, timeout_ms=10000)
                    else:
                        while True:
                            pump(wx)
                    phases.append("continued")
                except AssertionError:
                    phases.append("cancelled")
                    with pytest.raises(AssertionError, match="exceeded its deadline"):
                        wait_until(wx, lambda: True)
                    dialog.EndModal(wx.ID_ABORT)

            wx.CallAfter(interact)
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()

        # Earlier hooks may skip a modal after a newer hook's Enter, without Exit.
        blocked = wx.Dialog(frame, title="Blocked")
        try:
            assert blocked.ShowModal() == wx.ID_CANCEL and not blocked.IsModal()
        finally:
            blocked.Destroy()
        show(0, frame)

    try:
        with pytest.raises(AssertionError, match="exceeded its deadline"):
            run_native(exercise, after_events=lambda *_args: phases.append("after"))
        assert armed == [True]
        assert endings == [str(level) for level in reversed(range(depth))]
        assert phases == ([] if dispatch == "modal" else ["cancelled"])
        assert all(not window for window in created)
        assert wx.GetApp() is app and app.GetTopWindow() is host and host.IsEnabled()
        assert set(wx.GetTopLevelWindows()) == {host}
        run_native(lambda *_args: phases.append("reused"))
        assert phases[-1] == "reused" and app.GetTopWindow() is host
    finally:
        hook.Unregister()
        for timer in watchdog:
            timer.Stop()
        for window in wx.GetTopLevelWindows():
            window.Destroy()
        wait_until(wx, lambda: not wx.GetTopLevelWindows())
        app.Destroy()


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("stage", ["queued", "construct", "show", "register", "timer"])
@pytest.mark.parametrize("existing", [False, True])
def test_native_setup_failure_restores_existing_application(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    existing: bool,
) -> None:
    """Setup failures need the same teardown guarantee as exercise failures."""
    if isolated_native(request, "KICAD_NATIVE_SETUP_CHILD"):
        return
    import wx

    app = wx.App(False) if existing else None
    host = wx.Frame(None, title="Existing host") if existing else None
    if app is not None:
        app.SetTopWindow(host)
    created = []
    frame_type, call_later = wx.Frame, wx.CallLater

    class Frame(frame_type):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if stage == "queued":
                wx.CallAfter(lambda: created.append(frame_type(None)))
                raise RuntimeError("setup failure")
            super().__init__(*args, **kwargs)
            created.append(self)
            if stage == "construct":
                raise RuntimeError("setup failure")

        def Show(self, show: bool = True) -> bool:
            if stage == "show":
                raise RuntimeError("setup failure")
            return super().Show(show)

    def schedule(delay: int, callback: Any, *args: Any, **kwargs: Any) -> Any:
        if delay == 20000:
            raise RuntimeError("setup failure")
        return call_later(delay, callback, *args, **kwargs)

    class Hook(wx.ModalDialogHook):
        def Register(self) -> None:
            raise RuntimeError("setup failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(wx, "Frame", Frame)
            if stage == "timer":
                patch.setattr(wx, "CallLater", schedule)
            if stage == "register":
                patch.setattr(wx, "ModalDialogHook", Hook)
            with pytest.raises(RuntimeError, match="setup failure"):
                run_native(
                    lambda *_args: pytest.fail("exercise ran after setup failure")
                )
        assert bool(created) is (stage != "register")
        assert all(not window for window in created)
        assert wx.GetApp() is app
        assert set(wx.GetTopLevelWindows()) == ({host} if existing else set())
        if app is not None:
            assert app.GetTopWindow() is host
        run_native(lambda *_args: None)
        assert wx.GetApp() is app
    finally:
        remaining = wx.GetApp()
        if remaining is not None:
            pump(wx)
            for window in wx.GetTopLevelWindows():
                window.Destroy()
            wait_until(wx, lambda: not wx.GetTopLevelWindows())
            remaining.Destroy()


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("primary", ["", "exercise", "setup"])
def test_native_cleanup_cancels_modal_entry_and_destroys_queued_windows(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    primary: str,
) -> None:
    """Native destruction must not open an unbounded modal or leave late windows."""
    if isolated_native(request, "KICAD_NATIVE_CLEANUP_MODAL_CHILD"):
        return
    import wx

    app = wx.App(False) if existing else None
    host = wx.Frame(None, title="Existing host") if existing else None
    if app is not None:
        app.SetTopWindow(host)
    created, results = [], []

    def attach(frame: Any) -> None:
        created.append(frame)

        def destroy(event: Any) -> None:
            dialog = wx.Dialog(None, title="Cleanup modal")
            created.append(dialog)
            results.append(dialog.ShowModal())
            dialog.Destroy()
            wx.CallAfter(
                lambda: created.append(wx.Frame(None, title="Queued cleanup window"))
            )
            event.Skip()

        frame.Bind(wx.EVT_WINDOW_DESTROY, destroy)

    def exercise(frame: Any, _wx: Any) -> None:
        attach(frame)
        if primary == "exercise":
            raise AssertionError("primary exercise failure")

    class Frame(wx.Frame):
        def Show(self, show: bool = True) -> bool:
            attach(self)
            raise RuntimeError("primary setup failure")

    try:
        with monkeypatch.context() as patch:
            if primary == "setup":
                patch.setattr(wx, "Frame", Frame)
            if primary:
                with pytest.raises(
                    RuntimeError if primary == "setup" else AssertionError,
                    match=f"primary {primary} failure",
                ):
                    run_native(exercise)
            else:
                run_native(exercise)
        assert results == [wx.ID_CANCEL]
        assert len(created) == 3 and all(not window for window in created)
        assert wx.GetApp() is app
        assert set(wx.GetTopLevelWindows()) == ({host} if existing else set())
        run_native(lambda *_args: None)
        if app is not None:
            assert app.GetTopWindow() is host
    finally:
        remaining = wx.GetApp()
        if remaining is not None:
            for window in wx.GetTopLevelWindows():
                remaining.ScheduleForDestruction(window)
            wait_until(wx, lambda: not wx.GetTopLevelWindows())
            remaining.Destroy()


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("failure", ["raise", "refuse"])
def test_native_cleanup_failure_preserves_primary_and_cleans_other_windows(
    request: pytest.FixtureRequest, primary: bool, failure: str
) -> None:
    """One failing Destroy override cannot skip other native windows or mask failure."""
    if isolated_native(request, "KICAD_NATIVE_CLEANUP_ERROR_CHILD"):
        return
    import wx

    app = wx.App(False)
    host = wx.Frame(None, title="Existing host")
    app.SetTopWindow(host)
    created, calls = [], []

    class FailingFrame(wx.Frame):
        def Destroy(self) -> bool:
            calls.append(self.GetTitle())
            if failure == "refuse":
                return False
            raise RuntimeError("cleanup destruction failure")

    def exercise(frame: Any, _wx: Any) -> None:
        created.extend(
            (
                frame,
                FailingFrame(None, title="First"),
                FailingFrame(None, title="Second"),
                wx.Frame(None),
            )
        )
        if primary:
            raise AssertionError("primary exercise failure")

    try:
        error = AssertionError if primary or failure == "refuse" else RuntimeError
        message = (
            "primary exercise failure"
            if primary
            else "refused destruction"
            if failure == "refuse"
            else "cleanup destruction failure"
        )
        with pytest.raises(error, match=message):
            run_native(exercise)
        assert sorted(calls) == ["First", "Second"]
        assert all(not window for window in created)
        assert wx.GetApp() is app and app.GetTopWindow() is host
        assert set(wx.GetTopLevelWindows()) == {host}
        run_native(lambda *_args: None)
        assert app.GetTopWindow() is host
    finally:
        for window in wx.GetTopLevelWindows():
            app.ScheduleForDestruction(window)
        wait_until(wx, lambda: not wx.GetTopLevelWindows())
        app.Destroy()


@pytest.mark.native_wx
@pytest.mark.skipif(
    os.environ.get("KICAD_JLCPCB_NATIVE_TESTS") != "1",
    reason="native wx windows require an enabled desktop session",
)
@pytest.mark.parametrize("failure", [False, True])
def test_native_owned_app_finalizer_preserves_later_capture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    """Collect a destroyed real app during a later capture without resetting streams."""
    if isolated_native(request, "KICAD_NATIVE_STDIO_CHILD"):
        return
    import wx

    retained, references = [], []

    def exercise(_frame: Any, _wx: Any) -> None:
        app = wx.GetApp()
        app.capture_cycle = app
        retained.append(app)
        references.append(weakref.ref(app))
        if failure:
            raise AssertionError("expected exercise failure")

    if failure:
        with pytest.raises(AssertionError, match="expected exercise failure"):
            run_native(exercise)
    else:
        run_native(exercise)
    assert wx.GetApp() is None and references[0]() is not None
    output, errors = io.StringIO(), io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", output)
        patch.setattr(sys, "stderr", errors)
        retained.clear()
        gc.collect()
        assert references[0]() is None
        assert sys.stdout is output and sys.stderr is errors
        sys.stdout.write("later stdout\n")
        sys.stderr.write("later stderr\n")
        assert output.getvalue() == "later stdout\n"
        assert errors.getvalue() == "later stderr\n"


@pytest.mark.parametrize("failure", ["", "down", "press", "target"])
def test_physical_drag_waits_for_delivery_and_releases_mouse_on_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Posting input cannot authorize motion, and a failed wait must release it."""
    from . import test_variant_matrix_native as matrix

    events, pending, delivered, budgets = [], [], [], []
    clock = [100.0]
    state = SimpleNamespace(capture=False, origin=0, waits=0)
    view = SimpleNamespace(_header_drag=None)

    def move(x: int, _y: int) -> bool:
        events.append(f"move:{x}")
        if x != 10:
            assert state.capture, "Motion preceded actual press delivery"

            def apply() -> None:
                view._header_drag.active = True
                view._header_drag.valid = True
                view._header_drag.before = "A" if delivered else "C"
                delivered.append(True)
                state.origin = 200

            pending.append(apply)
        return True

    def press() -> bool:
        events.append("down")

        def apply() -> None:
            state.capture, state.origin = True, 100
            view._header_drag = SimpleNamespace(variant="B", active=False)

        pending.append(apply)
        return failure != "down"

    def release() -> bool:
        events.append("up")
        state.capture = False
        return True

    def wait(_wx: Any, predicate: Any, **_kwargs: Any) -> None:
        assert not predicate(), "The native event must be delivered before continuing"
        budgets.append(_kwargs["timeout_ms"])
        state.waits += 1
        if failure == (
            "press" if state.waits == 1 else "target" if state.waits == 3 else None
        ):
            raise AssertionError("delivery failure")
        pending.pop(0)()
        clock[0] += 0.25
        assert predicate()

    def locate(published: bool, _timeout_ms: int) -> tuple[Any, Any]:
        return (
            SimpleNamespace(x=10 + state.origin, y=5),
            SimpleNamespace(x=20 + state.origin + int(published), y=5),
        )

    simulator = SimpleNamespace(MouseMove=move, MouseDown=press, MouseUp=release)
    wx = SimpleNamespace(Platform="__WXGTK__", UIActionSimulator=lambda: simulator)
    header = SimpleNamespace(HasCapture=lambda: state.capture)
    monkeypatch.setattr(matrix, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(matrix, "wait_until", wait)
    if failure:
        with pytest.raises(
            AssertionError, match="delivery failure" if failure != "down" else None
        ):
            matrix._physical_header_drag(wx, view, header, delivered, locate)
    else:
        matrix._physical_header_drag(wx, view, header, delivered, locate)
    assert events == (
        ["move:10", "down", "up"]
        if failure in ("down", "press")
        else ["move:10", "down", "move:120", "move:221", "up"]
    )
    assert not state.capture
    assert budgets == (
        []
        if failure == "down"
        else [3000]
        if failure == "press"
        else [3000, 2750, 2500]
    )


def _assert_worker_resources_released(states: list[dict[str, str]]) -> None:
    """Verify the controller releases displays and state even after a crash."""
    import ctypes

    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    for state in states:
        assert not Path(state["XDG_CONFIG_HOME"]).parent.exists()
        assert not x11.XOpenDisplay(state["DISPLAY"].encode())


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("Xvfb") is None,
    reason="Requires Linux Xvfb",
)
def test_parallel_native_workers_isolate_desktops_and_reuse_them_in_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh wx startup and exact-node children share a desktop that cannot reset."""
    pytest.importorskip("xdist")
    pytest.importorskip("wx")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    result = run_lane(
        tmp_path,
        monkeypatch,
        "wx",
        "import ctypes, json, os, subprocess, sys, tempfile\n"
        "from pathlib import Path\n"
        "@pytest.mark.native_wx\n"
        "def test_native(request):\n"
        "    if os.environ.get('EXACT_CHILD') != '1':\n"
        "        parent = os.getppid()\n"
        "        servers = []\n"
        "        for pid in Path(f'/proc/{parent}/task/{parent}/children').read_text().split():\n"
        "            try:\n"
        "                command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')\n"
        "            except FileNotFoundError:\n"
        "                continue\n"
        "            if command[0].rsplit(b'/', 1)[-1] == b'Xvfb':\n"
        "                servers.append(command)\n"
        "        assert len(servers) == 2 and all(b'-noreset' in cmd for cmd in servers), servers\n"
        "    x11 = ctypes.CDLL('libX11.so.6')\n"
        "    x11.XOpenDisplay.restype = ctypes.c_void_p\n"
        "    display = x11.XOpenDisplay(None)\n"
        "    assert display, 'Private display is unavailable'\n"
        "    x11.XCloseDisplay(ctypes.c_void_p(display))\n"
        "    import wx\n"
        "    assert wx.GetApp() is None\n"
        "    app = wx.App(False)\n"
        "    try:\n"
        "        assert wx.GetApp() is app\n"
        "    finally:\n"
        "        app.Destroy()\n"
        "    state = {key: os.environ[key] for key in ('DISPLAY', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'KICAD_CONFIG_HOME', 'TMPDIR')}\n"
        "    assert request.config.getini('cache_dir') == state['XDG_CACHE_HOME']\n"
        "    assert request.config.cache.mkdir('worker-probe').is_relative_to(Path(state['XDG_CACHE_HOME']))\n"
        "    assert tempfile.gettempdir() == state['TMPDIR']\n"
        "    assert all(Path(value).stat().st_mode & 0o777 == 0o700 for key, value in state.items() if key != 'DISPLAY')\n"
        "    if os.environ.get('EXACT_CHILD') == '1':\n"
        "        assert not hasattr(request.config, 'workerinput')\n"
        "        assert state == json.loads(os.environ['EXPECTED_STATE'])\n"
        "        return\n"
        "    child = subprocess.run([sys.executable, '-m', 'pytest', '-q', request.node.nodeid],\n"
        "        env=dict(os.environ, EXACT_CHILD='1', EXPECTED_STATE=json.dumps(state)),\n"
        "        capture_output=True, text=True, timeout=10)\n"
        "    assert child.returncode == 0, child.stdout + child.stderr\n"
        "    Path(__file__).with_suffix('.json').write_text(json.dumps(state))\n",
        parallel=True,
        real_wx=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    states = [json.loads(path.read_text()) for path in tmp_path.glob("test_*.json")]
    assert len(states) == 2
    for key in states[0]:
        assert states[0][key] != states[1][key], (key, states)
    _assert_worker_resources_released(states)


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("Xvfb") is None,
    reason="Requires Linux Xvfb",
)
@pytest.mark.parametrize(
    "source, message",
    [
        (
            "@pytest.mark.native_wx\ndef test_native(): assert False, 'probe assertion'\n",
            "probe assertion",
        ),
        (
            "@pytest.fixture\ndef broken(): raise RuntimeError('probe fixture')\n"
            "@pytest.mark.native_wx\ndef test_native(broken): pass\n",
            "probe fixture",
        ),
        ("raise RuntimeError('probe collection')\n", "probe collection"),
        (
            "@pytest.mark.native_wx\ndef test_native(): pytest.skip('probe skip')\n",
            "Required native test skipped",
        ),
        (
            "import json, os\nfrom pathlib import Path\n@pytest.mark.native_wx\n"
            "def test_native():\n"
            "    Path(__file__).with_suffix('.json').write_text(json.dumps({key: os.environ[key] for key in ('DISPLAY', 'XDG_CONFIG_HOME')}))\n"
            "    os._exit(4)\n",
            "crashed",
        ),
    ],
)
def test_parallel_native_worker_failures_fail_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, message: str
) -> None:
    """Standard workers propagate phase failures, required skips and crashes."""
    pytest.importorskip("xdist")
    result = run_lane(tmp_path, monkeypatch, "wx", source, parallel=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert message in result.stdout + result.stderr
    if message == "crashed":
        states = [json.loads(path.read_text()) for path in tmp_path.glob("test_*.json")]
        assert len(states) == 2
        _assert_worker_resources_released(states)
