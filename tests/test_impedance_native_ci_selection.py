"""Verify native feature lane selection and retained failure evidence."""

from collections import Counter
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CASES = {
    "tests/test_impedance_capture_examples.py": 20,
    "tests/test_impedance_connector_captures.py": 2,
    "tests/test_impedance_metadata_native.py": 15,
    "tests/test_impedance_netclass_fixtures.py": 28,
    "tests/test_impedance_performance_native.py": 3,
    "tests/test_rf_impedance_fixture_realism.py": 16,
    "tests/test_rf_impedance_native.py": 168,
    "tests/test_rf_impedance_showcases.py": 2,
    "tests/test_copper_zone_preview.py::test_native_asymmetric_copper_and_teardrop_align_in_pixels": 6,
    "tests/test_copper_zones.py::test_native_metadata_survives_saved_board_reopening": 2,
    "tests/test_copper_zones.py::test_native_completed_empty_teardrop_is_logged_after_real_fabrication_fill": 1,
    "tests/test_copper_zones.py::test_native_empty_teardrop_reporting_reads_changed_flags_without_refilling": 1,
}


def run_lane(
    directory: Path,
    lane: str = "native-kicad-10",
    *,
    status: int = 0,
    environment: Optional[dict[str, str]] = None,
) -> Any:
    """Run the real dispatcher with labeled command stand-ins, retaining its shell behavior."""
    directory.mkdir(parents=True, exist_ok=True)
    binaries = directory / "bin"
    binaries.mkdir()
    scripts = {
        "id": "echo 1001\n",
        "dpkg": "echo amd64\n",
        "timeout": (
            'printf "%s\\n" "$1" >> "$STUB_ROOT/timeouts"\n'
            'if [ "$STUB_STATUS" -ge 124 ]; then\n'
            ' echo "native stdout"; echo "native stderr" >&2; exit "$STUB_STATUS"\n'
            'fi\nshift\nexec "$@"\n'
        ),
        "xvfb-run": 'export DISPLAY=:lane-fixture\nshift 3\nexec "$@"\n',
        "openbox": 'touch "$STUB_ROOT/wm-ready"\nexec sleep 60\n',
        "xprop": (
            '[ -f "$STUB_ROOT/wm-ready" ] || exit 1\n'
            'echo "_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x123"\n'
        ),
    }
    for name, body in scripts.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
    native = directory / ".venv/bin/python"
    native.parent.mkdir(parents=True)
    native.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2]==['-m','pip']: sys.exit(0)\n"
        "root=pathlib.Path(os.environ['STUB_ROOT'])\n"
        "if args[0]=='scripts/linux_native_impedance_smoke.py':\n"
        " assert os.environ.get('DISPLAY')==':lane-fixture', 'Smoke needs the outer display'\n"
        " assert (root/'wm-ready').is_file(), 'Smoke started before its window manager'\n"
        "with (root/'calls.jsonl').open('a') as log: log.write(json.dumps(args)+'\\n')\n"
        "for flag in ('--artifacts','--output'):\n"
        " if flag in args:\n"
        "  output=pathlib.Path(args[args.index(flag)+1]);output.mkdir(exist_ok=True)\n"
        "  (output/'command.json').write_text(json.dumps(args))\n"
        "proof='tests/test_impedance_source_evidence_native.py' in args\n"
        "probe='scripts/native_impedance_source_probe.py' in args\n"
        "print('proof output' if proof else 'probe output' if probe else 'native stdout')\n"
        "if not proof and not probe: print('native stderr',file=sys.stderr)\n"
        "sys.exit(int(os.environ['PROOF_EXIT' if proof else 'PROBE_EXIT' if probe else 'STUB_STATUS']))\n",
        encoding="utf-8",
    )
    native.chmod(0o755)
    artifacts = directory / ".cache/native-tests" / lane
    artifacts.mkdir(parents=True)
    values = dict(os.environ)
    for name in (
        "NATIVE_GENERATE_BENCHMARK",
        "NATIVE_SMOKE_BENCHMARK",
        "NATIVE_SOURCE_SNAPSHOT_PROBE",
    ):
        values[name] = "0"
    values.update(
        PATH=str(binaries) + os.pathsep + values["PATH"],
        STUB_ROOT=str(directory),
        STUB_STATUS=str(status),
        PROOF_EXIT="0",
        PROBE_EXIT="0",
    )
    values.update(environment or {})
    process = subprocess.run(
        [
            "bash",
            "-c",
            '/usr/bin/python3() { :; }; source "$1" --inside "$2" amd64',
            "lane-fixture",
            str(ROOT / "scripts/run_test_lane.sh"),
            lane,
        ],
        cwd=directory,
        env=values,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    calls = directory / "calls.jsonl"
    timeouts = directory / "timeouts"
    return SimpleNamespace(
        process=process,
        calls=[json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else [],
        timeouts=timeouts.read_text().splitlines() if timeouts.exists() else [],
        artifacts=artifacts,
    )


@pytest.mark.parametrize("major", ["7"])
@pytest.mark.parametrize("status", [0, 1, 2, 124, 137])
def test_native_log_capture_preserves_test_runner_and_timeout_failures(
    tmp_path: Path, major: str, status: int
) -> None:
    """Retain complete logs and a failed lane through pipelines and external-output cleanup."""
    run = run_lane(tmp_path, f"native-kicad-{major}", status=status)
    assert run.process.returncode == status, run.process.stderr
    assert run.timeouts == ["900s"]
    log = "pytest.log"
    lines = (run.artifacts / log).read_text().splitlines()
    assert sorted(lines) == ["native stderr", "native stdout"]
    assert not run.process.stderr
    assert bool(run.calls) == (status < 124)


def test_native_controls_keep_parallelism_with_feature_review_allowance(
    tmp_path: Path,
) -> None:
    """The longer feature review allowance keeps native workers and required wx coverage."""
    run = run_lane(tmp_path, "native-wx-0")
    assert run.process.returncode == 0, run.process.stderr
    assert run.timeouts == ["900s"]
    arguments = run.calls[0]
    assert "--require-native=wx" in arguments
    assert arguments[arguments.index("-n") + 1] == "auto"
    assert "--maxprocesses=4" in arguments
    assert "--dist=loadfile" in arguments
    assert (
        arguments[arguments.index("-m", 2) + 1] == "not native_kicad and not os_input"
    )


def _fixture_case(nodeid: str) -> str:
    """Recognize required cases while preserving fallbacks in mixed modules."""
    case = nodeid.split("[", 1)[0]
    if case in FIXTURE_CASES:
        return case
    path = nodeid.split("::", 1)[0]
    return path if path in FIXTURE_CASES else ""


@pytest.fixture(scope="module")
def matrix_selections(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Resolve every compatibility selector from the actual lane dispatcher."""
    directory = tmp_path_factory.mktemp("native-lane-selection")
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    majors = re.findall(r"^          - lane: native-kicad-(\d+)$", workflow, re.M)
    assert set(majors) == {"7", "8", "9", "10"}
    result = {}
    for major in majors:
        lane = run_lane(directory / major, f"native-kicad-{major}")
        assert lane.process.returncode == 0, lane.process.stderr
        arguments = next(call for call in lane.calls if call[:2] == ["-m", "pytest"])
        assert f"--kicad-major={major}" in arguments
        result[major] = arguments[arguments.index("-m", 2) + 1]
    return result


@pytest.fixture(scope="module")
def native_collection(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str], dict[str, Any]]:
    """Ask real pytest collection/deselection to evaluate the workflow expressions."""
    directory = tmp_path_factory.mktemp("native-impedance-selection")
    plugin = directory / "_impedance_selection_probe.py"
    plugin.write_text(
        """import json
import os
from pathlib import Path
from typing import Any
import pytest

all_items = []

@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(items: list[Any]) -> Any:
    all_items[:] = items
    yield

def pytest_collection_finish(session: Any) -> None:
    Path(os.environ["IMPEDANCE_SELECTION_REPORT"]).write_text(json.dumps({
        "items": [{"nodeid": item.nodeid, "markers": [marker.name for marker in item.iter_markers()]} for item in all_items],
        "selected": [item.nodeid for item in session.items],
    }))
""",
        encoding="utf-8",
    )
    cache: dict[str, dict[str, Any]] = {}
    native_modules = sorted(
        str(path.relative_to(ROOT))
        for directory in ("tests", "common", "dblib")
        for path in (ROOT / directory).rglob("test*.py")
        if "native_kicad" in path.read_text(encoding="utf-8")
    )

    def collect(expression: str) -> dict[str, Any]:
        """Collect without inheriting the caller's required-runtime test mode."""
        if expression not in cache:
            report = directory / f"selection-{len(cache)}.json"
            environment = dict(os.environ)
            environment.pop("KICAD_JLCPCB_REQUIRE_NATIVE", None)
            environment["PYTEST_ADDOPTS"] = ""
            environment["PYTHONPATH"] = os.pathsep.join((str(directory), str(ROOT)))
            environment["IMPEDANCE_SELECTION_REPORT"] = str(report)
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    plugin.stem,
                    "-m",
                    expression,
                    *native_modules,
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert process.returncode == 0, process.stdout + process.stderr
            cache[expression] = json.loads(report.read_text(encoding="utf-8"))
        return cache[expression]

    return collect


def test_only_documented_native_fixture_cases_require_kicad10(
    native_collection: Callable[[str], dict[str, Any]],
) -> None:
    """Do not accidentally mark unit helpers or supported native fallback checks."""
    collection = native_collection("native_kicad")
    expected = {
        item["nodeid"]
        for item in collection["items"]
        if "native_kicad" in item["markers"] and _fixture_case(item["nodeid"])
    }
    assert Counter(_fixture_case(node) for node in expected) == FIXTURE_CASES
    marked = {
        item["nodeid"]
        for item in collection["items"]
        if "kicad10_impedance" in item["markers"]
    }
    assert marked == expected


@pytest.mark.parametrize("major", ["7", "8", "9", "10"])
def test_native_matrix_selects_all_supported_checks_and_required_fixture_cases(
    native_collection: Callable[[str], dict[str, Any]],
    matrix_selections: dict[str, str],
    major: str,
) -> None:
    """Evaluate actual CI selections; old lanes retain every other native test."""
    all_native = set(native_collection("native_kicad")["selected"])
    fixture_cases = {node for node in all_native if _fixture_case(node)}
    supported = all_native - fixture_cases
    assert len(fixture_cases) == sum(FIXTURE_CASES.values())
    assert supported, "Existing native compatibility/fallback coverage must remain"
    selected = set(native_collection(matrix_selections[major])["selected"])
    assert selected == (all_native if major == "10" else supported)
