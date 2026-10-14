"""Exercise required native lanes through isolated pytest processes."""

import json
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
    version: str = "10.0.6",
    kicad_major: Optional[str] = None,  # noqa: UP045
) -> subprocess.CompletedProcess[str]:
    """Run the real pytest hooks with isolated, deliberately limited bindings.

    ``broken="variants"`` gives a KiCad without the design-variant API, as
    KiCad 9 is; ``kicad_major`` is the lane's ``--kicad-major``.
    """
    hooks = Path(__file__).parents[1] / "conftest.py"
    shutil.copyfile(hooks, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    native_wx\n    native_kicad\n    os_input\n"
        "    kicad_variants\n    kicad_standalone_drc\n    kicad_golden\n"
    )
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
    if mode and not cli:
        monkeypatch.setenv("KICAD_JLCPCB_REQUIRE_NATIVE", mode)
    command = [sys.executable, "-m", "pytest", "-q", "-rs"]
    if cli:
        command.append(f"--require-native={mode}")
    if kicad_major:
        command.append(f"--kicad-major={kicad_major}")
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
    """KiCad 7 and 8 cannot open the KiCad 9 fixture boards, so none are recorded."""
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
