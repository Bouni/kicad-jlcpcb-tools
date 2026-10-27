"""Keep strict Python budgets after bulk work without changing selected coverage."""

from collections import Counter
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPACING = "tests/test_impedance_spacing_performance.py"
WORKFLOW = "tests/test_impedance_performance_budget.py"
NATIVE = "tests/test_impedance_performance_native.py"
BULK_A = "tests/test_a_bulk.py"
BULK_B = "tests/test_b_bulk.py"
SPACING_CASES = [
    f"{SPACING}::test_spacing[second]",
    f"{SPACING}::test_spacing[first]",
    f"{SPACING}::test_spacing_other",
]
WORKFLOW_CASES = [
    f"{WORKFLOW}::test_workflow[save]",
    f"{WORKFLOW}::test_workflow[open]",
]
NATIVE_CASE = f"{NATIVE}::test_native_budget"
BULK_CASES = [
    f"{BULK_B}::test_bulk_z",
    f"{BULK_B}::test_bulk_a",
    f"{BULK_B}::test_input",
    f"{BULK_B}::test_kicad",
]
LAST_BULK = f"{BULK_A}::test_last_bulk"
MARKS = {
    **{node: ["chosen"] for node in SPACING_CASES[:2] + WORKFLOW_CASES},
    SPACING_CASES[2]: [],
    NATIVE_CASE: ["chosen", "native_wx"],
    BULK_CASES[0]: ["chosen"],
    BULK_CASES[1]: [],
    BULK_CASES[2]: ["native_wx", "os_input"],
    BULK_CASES[3]: ["native_kicad"],
    LAST_BULK: [],
}


@pytest.fixture
def ordered_suite(tmp_path: Path) -> Path:
    """Run actual repository hooks with explicitly limited native binding stand-ins."""
    suite = tmp_path / "suite"
    tests = suite / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(ROOT / "conftest.py", suite / "conftest.py")
    (suite / "pytest.ini").write_text(
        "[pytest]\nmarkers=\n chosen\n native_wx\n native_kicad\n os_input\n"
    )
    (suite / "wx").mkdir()
    (suite / "wx" / "__init__.py").write_text(
        "def version(): return 'ordering-test binding stand-in'\n"
    )
    (suite / "wx" / "grid.py").write_text("class Grid: pass\n")
    # Ordering uses no real GUI. Native-lane tests cover the actual desktops.
    (suite / "pyvirtualdisplay.py").write_text(
        "class Display:\n"
        " def __init__(self, **kwargs: object) -> None: self.new_display_var=':ordering'\n"
        " def start(self) -> None: pass\n"
        " def stop(self) -> None: pass\n"
    )
    (tests / "conftest.py").write_text(
        "import json,os,pytest\n"
        "from pathlib import Path\n"
        "root=Path(__file__).parent\n"
        "def pytest_collection_finish(session: pytest.Session) -> None:\n"
        " names={'chosen','native_wx','native_kicad','os_input'}\n"
        " (root/'collection.json').write_text(json.dumps({\n"
        "  'required':session.config.getoption('require_native'),\n"
        "  'required_env':os.environ.get('KICAD_JLCPCB_REQUIRE_NATIVE'),\n"
        "  'native_env':os.environ.get('KICAD_JLCPCB_NATIVE_TESTS'),\n"
        "  'worker':getattr(session.config,'workerinput',{}).get('workerid'),\n"
        "  'selected':[item.nodeid for item in session.items],\n"
        "  'marks':{item.nodeid:sorted(marker.name for marker in item.iter_markers() if marker.name in names) for item in session.items},\n"
        " }))\n"
        "def pytest_runtest_call(item: pytest.Item) -> None:\n"
        " with (root/'executed.txt').open('a') as log: log.write(item.nodeid+'\\n')\n"
    )
    modules = {
        SPACING: (
            "@pytest.mark.chosen\n"
            "@pytest.mark.parametrize('sample',['second','first'])\n"
            "def test_spacing(sample): pass\n"
            "def test_spacing_other(): pass\n"
        ),
        WORKFLOW: (
            "@pytest.mark.chosen\n"
            "@pytest.mark.parametrize('sample',['save','open'])\n"
            "def test_workflow(sample): pass\n"
        ),
        NATIVE: (
            "@pytest.mark.native_wx\n@pytest.mark.chosen\n"
            "def test_native_budget(): pass\n"
        ),
        BULK_B: (
            "@pytest.mark.chosen\ndef test_bulk_z(): pass\n"
            "def test_bulk_a(): pass\n"
            "@pytest.mark.native_wx\n@pytest.mark.os_input\n"
            "def test_input(): pass\n"
            "@pytest.mark.native_kicad\ndef test_kicad(): pass\n"
        ),
        BULK_A: "def test_last_bulk(): pass\n",
    }
    for path, source in modules.items():
        (suite / path).write_text("import pytest\n" + source)
    return suite


@pytest.mark.parametrize(
    "profile",
    [
        "ordinary",
        "ordinary-selection",
        "chosen",
        "wx-xdist",
        "native-child",
        "budget-node",
    ],
)
def test_actual_pytest_keeps_budget_modules_at_a_stable_tail(
    ordered_suite: Path, tmp_path: Path, profile: str
) -> None:
    """Execution, marks and worker collection agree after filtering and node reruns."""
    arguments = [SPACING, BULK_B, WORKFLOW, NATIVE, BULK_A]
    expected = BULK_CASES + [NATIVE_CASE, LAST_BULK] + SPACING_CASES + WORKFLOW_CASES
    required = None
    junit = tmp_path / "pytest.xml"
    command = [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"]
    if profile == "ordinary-selection":
        command += ["-m", "not native_wx and not native_kicad"]
        expected = BULK_CASES[:2] + [LAST_BULK] + SPACING_CASES + WORKFLOW_CASES
    elif profile == "chosen":
        command += ["-m", "chosen"]
        expected = [BULK_CASES[0], NATIVE_CASE] + SPACING_CASES[:2] + WORKFLOW_CASES
    elif profile == "wx-xdist":
        pytest.importorskip(
            "xdist", reason="Ordering through workers requires pytest-xdist"
        )
        required = "wx"
        # Worker module arguments are alphabetic; the two budget modules keep
        # that original relative order after all selected bulk modules.
        expected = (
            [LAST_BULK, BULK_CASES[0], NATIVE_CASE] + WORKFLOW_CASES + SPACING_CASES
        )
        arguments = sorted(expected, key=lambda node: node.split("::")[0])
        command += [
            "-p",
            "xdist.plugin",
            "-n",
            "1",
            "--dist=loadfile",
            "--no-loadscope-reorder",
            "--max-worker-restart=0",
            "--require-native=wx",
        ]
    elif profile == "native-child":
        required = "wx"
        command += ["--require-native=wx"]
        arguments, expected = [NATIVE_CASE], [NATIVE_CASE]
    elif profile == "budget-node":
        arguments, expected = [SPACING_CASES[1]], [SPACING_CASES[1]]
    environment = dict(
        os.environ,
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
        PYTHONPATH=str(ordered_suite),
    )
    for key in (
        "KICAD_JLCPCB_REQUIRE_NATIVE",
        "KICAD_JLCPCB_NATIVE_TESTS",
        "KICAD_JLCPCB_KICAD_MAJOR",
    ):
        environment.pop(key, None)
    completed = subprocess.run(
        command + arguments,
        cwd=ordered_suite,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads((ordered_suite / "tests" / "collection.json").read_text())
    executed = (ordered_suite / "tests" / "executed.txt").read_text().splitlines()
    assert executed == expected
    assert observed["selected"] == expected
    assert Counter(executed) == Counter(expected)
    assert observed["marks"] == {node: MARKS[node] for node in expected}
    assert observed["required"] == observed["required_env"] == required
    assert observed["native_env"] == ("1" if required else None)
    cases = list(ET.parse(junit).iter("testcase"))  # noqa: S314 - pytest-owned evidence
    assert len(cases) == len(expected)
    assert not any(
        case.find(outcome) is not None
        for case in cases
        for outcome in ("skipped", "failure", "error")
    )
    for case in cases:
        workers = [
            prop.get("value")
            for prop in case.findall("./properties/property")
            if prop.get("name") == "native_worker_id"
        ]
        assert workers == (["gw0"] if profile == "wx-xdist" else [])
    assert observed["worker"] == ("gw0" if profile == "wx-xdist" else None)


@pytest.fixture
def phased_suite(ordered_suite: Path) -> Path:
    """Exercise the real lane shell and two real workers without native GUI work."""
    pytest.importorskip("xdist", reason="Phase isolation requires real pytest workers")
    suite = ordered_suite
    for module in (suite / "tests").glob("test_*.py"):
        module.unlink()
    (suite / "tests" / "conftest.py").write_text(
        "import json,os,pytest,time\n"
        "from pathlib import Path\n"
        "BUDGETS={'test_impedance_spacing_performance.py','test_impedance_performance_budget.py'}\n"
        "def event(kind,**values):\n"
        " values.update(kind=kind,pid=os.getpid(),at=time.monotonic(),epoch=time.time())\n"
        " with Path('events.jsonl').open('a') as stream: stream.write(json.dumps(values)+'\\n')\n"
        "def pytest_configure(config):\n"
        " event('configuration',worker=getattr(config,'workerinput',{}).get('workerid'),\n"
        "  required=config.getoption('require_native'),cap=config.getoption('maxprocesses'),distribution=config.getoption('dist'))\n"
        "def pytest_collection_finish(session):\n"
        " event('collection',worker=getattr(session.config,'workerinput',{}).get('workerid'),\n"
        "  required=session.config.getoption('require_native'),nodes=[i.nodeid for i in session.items])\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_call(item):\n"
        " phase='budget' if item.path.name in BUDGETS else 'bulk'\n"
        " event('start',phase=phase,node=item.nodeid)\n"
        " try: yield\n"
        " finally: event('finish',phase=phase,node=item.nodeid)\n"
        "def pytest_sessionfinish(session,exitstatus):\n"
        " event('session-finish',worker=getattr(session.config,'workerinput',{}).get('workerid'),required=session.config.getoption('require_native'),status=int(exitstatus))\n"
        "@pytest.fixture(autouse=True)\n"
        "def original_property(record_property): record_property('original-proof','retained')\n"
    )
    (suite / BULK_A).write_text(
        "import os,pytest,time\n"
        "from pathlib import Path\n"
        "@pytest.mark.native_wx\n"
        "def test_slow_bulk(capsys):\n"
        " if os.environ.get('PHASE_FAILURE')=='cancel-bulk':\n"
        "  Path('bulk-ready').touch()\n"
        "  time.sleep(60)\n"
        " time.sleep(1.5)\n"
        " assert os.environ.get('PHASE_FAILURE')!='bulk'\n"
    )
    (suite / BULK_B).write_text("def test_fast_bulk(): pass\n")
    for path in (SPACING, WORKFLOW):
        (suite / path).write_text(
            "import os,signal,sys,time\n"
            "from pathlib import Path\n"
            "def stop(*_):\n"
            " Path('cancel-ready').touch()\n"
            " while not Path('cancel-release').exists(): time.sleep(0.01)\n"
            " raise SystemExit(143)\n"
            "def test_budget(capsys):\n"
            " with capsys.disabled(): print('budget diagnostic output',flush=True)\n"
            " if os.environ.get('PHASE_FAILURE')=='cancel':\n"
            "  signal.signal(signal.SIGTERM,stop)\n"
            "  Path('budget-ready').touch()\n"
            " time.sleep(60 if os.environ.get('PHASE_FAILURE')=='cancel' else 0.1)\n"
            " assert os.environ.get('PHASE_FAILURE')!='budget'\n"
        )
    binaries = suite / "bin"
    binaries.mkdir()
    for name, body in {
        "id": "echo 1001\n",
        "dpkg": "echo amd64\n",
    }.items():
        command = binaries / name
        command.write_text("#!/bin/sh\n" + body)
        command.chmod(0o755)
    # Keep the production dispatcher and GNU timeout. Limit only this fixture's
    # auto worker count; the lane's existing four-worker cap is checked elsewhere.
    interpreter = suite / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os,runpy,sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2]==['-m','pip']: raise SystemExit(0)\n"
        "sys.argv=[sys.executable,*args]\n"
        "if args[:1]==['-m']:\n"
        " sys.argv=[sys.executable,*args[2:]]\n"
        " runpy.run_module(args[1],run_name='__main__')\n"
        "else:\n"
        " sys.argv=args\n"
        " runpy.run_path(args[0],run_name='__main__')\n"
    )
    interpreter.chmod(0o755)
    scripts = suite / "scripts"
    scripts.mkdir()
    for name in ("run_wx_tests.py", "native_impedance_support.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (suite / ".cache/native-tests/native-wx-0").mkdir(parents=True)
    return suite


def _phase_process(suite: Path, failure: str) -> subprocess.Popen[str]:
    """Run the actual dispatcher inside a limited dependency/setup stand-in."""
    environment = dict(
        os.environ,
        PATH=str(suite / "bin") + os.pathsep + os.environ["PATH"],
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="-p xdist.plugin",
        PYTEST_XDIST_AUTO_NUM_WORKERS="2",
        PYTHONPATH=str(suite),
        PHASE_FAILURE=failure,
    )
    for key in (
        "KICAD_JLCPCB_REQUIRE_NATIVE",
        "KICAD_JLCPCB_NATIVE_TESTS",
        "KICAD_JLCPCB_KICAD_MAJOR",
    ):
        environment.pop(key, None)
    return subprocess.Popen(
        [
            "bash",
            "-c",
            '/usr/bin/python3() { :; }; source "$1" --inside native-wx-0 amd64',
            "phase-fixture",
            str(ROOT / "scripts/run_test_lane.sh"),
        ],
        cwd=suite,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _phase_events(suite: Path) -> list[dict[str, Any]]:
    """Read only complete append-only execution receipts."""
    path = suite / "events.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def _stop_phase_process(process: subprocess.Popen[str]) -> None:
    """Reap fixture processes after failures as well as successful assertions."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


@pytest.mark.parametrize("failure", ["", "bulk", "budget"])
def test_actual_wx_lane_isolates_complete_serial_budget_phase(
    phased_suite: Path, failure: str
) -> None:
    """A real worker barrier preserves complete diagnostics and either phase's failure."""
    process = _phase_process(phased_suite, failure)
    try:
        output, errors = process.communicate(timeout=30)
    finally:
        _stop_phase_process(process)

    assert process.returncode == (1 if failure else 0), output + errors
    events = _phase_events(phased_suite)
    starts = [event for event in events if event["kind"] == "start"]
    finishes = [event for event in events if event["kind"] == "finish"]
    assert Counter(event["node"] for event in starts) == Counter(
        [
            f"{BULK_A}::test_slow_bulk",
            f"{BULK_B}::test_fast_bulk",
            f"{SPACING}::test_budget",
            f"{WORKFLOW}::test_budget",
        ]
    )
    bulk_pids = {event["pid"] for event in starts if event["phase"] == "bulk"}
    assert len(bulk_pids) == 2, events
    bulk_exits = [
        event
        for event in events
        if event["kind"] == "session-finish" and event["pid"] in bulk_pids
    ]
    assert {event["pid"] for event in bulk_exits} == bulk_pids
    budgets = [event for event in starts if event["phase"] == "budget"]
    assert min(event["at"] for event in budgets) > max(
        event["at"] for event in bulk_exits
    ), events
    assert len({event["pid"] for event in budgets}) == 1, events
    assert {event["pid"] for event in budgets}.isdisjoint(bulk_pids)
    first_finished = next(
        event for event in finishes if event["node"] == budgets[0]["node"]
    )
    assert first_finished["at"] < budgets[1]["at"]
    collections = [event for event in events if event["kind"] == "collection"]
    assert any(event["worker"] and event["required"] == "wx" for event in collections)
    assert any(
        event["kind"] == "configuration"
        and event["worker"] is None
        and event["required"] == "wx"
        and event["cap"] == 4
        and event["distribution"] == "loadfile"
        for event in events
    )
    controller_exit = next(
        event
        for event in events
        if event["kind"] == "session-finish"
        and event["worker"] is None
        and event["required"] == "wx"
    )
    assert min(event["at"] for event in budgets) > controller_exit["at"]
    assert collections[-1]["required"] is None
    artifacts = phased_suite / ".cache/native-tests/native-wx-0"
    phases = json.loads((artifacts / "phases.json").read_text())["phases"]
    assert phases[0]["finished_at"] <= phases[1]["started_at"]
    for phase in phases:
        calls = [
            event["epoch"]
            for event in starts
            if event["phase"] == ("budget" if phase["name"] == "budgets" else "bulk")
        ]
        assert phase["started_at"] <= min(calls) <= max(calls) <= phase["finished_at"]
    cases = list(ET.parse(artifacts / "pytest.xml").iter("testcase"))  # noqa: S314
    raw_cases = [
        case
        for phase in ("bulk", "budgets")
        for case in ET.parse(artifacts / f"pytest-{phase}.xml").iter("testcase")  # noqa: S314 - owned reports
    ]
    assert Counter(ET.tostring(case) for case in cases) == Counter(
        ET.tostring(case) for case in raw_cases
    )
    assert len(cases) == 4
    assert sum(case.find("failure") is not None for case in cases) == (
        2 if failure == "budget" else 1 if failure else 0
    )
    assert all(
        case.find("./properties/property[@name='original-proof']").get("value")
        == "retained"
        for case in cases
    )


@pytest.mark.parametrize("phase", ["bulk", "budget"])
def test_wx_budget_cancellation_reaps_active_process_and_retains_bulk_diagnostics(
    phased_suite: Path,
    phase: str,
) -> None:
    """Cancelling the real shell keeps the finished phase and terminates its active child."""
    process = _phase_process(
        phased_suite, "cancel-bulk" if phase == "bulk" else "cancel"
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            budgets = [
                event
                for event in _phase_events(phased_suite)
                if event["kind"] == "start" and event["phase"] == phase
            ]
            if budgets and (phased_suite / f"{phase}-ready").exists():
                break
            assert process.poll() is None
            assert time.monotonic() < deadline, "Budget phase did not become ready"
            time.sleep(0.02)
        artifacts = phased_suite / ".cache/native-tests/native-wx-0"
        helper_pid = json.loads((artifacts / "phases.json").read_text())["pid"]
        process.send_signal(signal.SIGTERM)
        if phase == "budget":
            while not (phased_suite / "cancel-ready").exists():
                assert time.monotonic() < deadline, "Child did not receive cancellation"
                time.sleep(0.02)
            os.kill(helper_pid, signal.SIGTERM)
            (phased_suite / "cancel-release").touch()
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 143, output + errors
        for pid in {
            event["pid"]
            for event in _phase_events(phased_suite)
            if event["kind"] == "configuration"
        }:
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline, f"Unreaped process {pid}"
                time.sleep(0.02)
        report_phase = "budgets" if phase == "budget" else "bulk"
        log = (artifacts / f"pytest-{report_phase}.log").read_text()
        assert ("budget diagnostic output" if phase == "budget" else "workers") in log
        if phase == "budget":
            assert (
                len(list(ET.parse(artifacts / "pytest-bulk.xml").iter("testcase"))) == 2  # noqa: S314 - owned pytest output
            )
        else:
            assert not any(
                event["kind"] == "start" and event["phase"] == "budget"
                for event in _phase_events(phased_suite)
            )
        assert "cancel" in (artifacts / "phases.json").read_text().lower()
    finally:
        _stop_phase_process(process)


@pytest.mark.parametrize(
    "defect", ["missing", "malformed", "count", "duplicate", "counter"]
)
def test_phase_merge_rejects_incomplete_reports(tmp_path: Path, defect: str) -> None:
    """Successful child exits cannot hide broken or overlapping phase evidence."""
    from scripts.run_wx_tests import merge_results

    report = tmp_path / "phase.xml"
    report.write_text(
        '<testsuite tests="1"><testcase classname="tests.bulk" name="test_one"/></testsuite>'
    )
    paths = [report]
    if defect == "missing":
        paths.append(tmp_path / "missing.xml")
    elif defect == "malformed":
        report.write_text("<testsuite")
    elif defect == "count":
        report.write_text(
            '<testsuite tests="2"><testcase classname="tests.bulk" name="test_one"/></testsuite>'
        )
    elif defect == "counter":
        report.write_text(
            '<testsuite tests="1" failures="1"><testcase classname="tests.bulk" name="test_one"/></testsuite>'
        )
    else:
        paths.append(report)
    with pytest.raises((OSError, ValueError, ET.ParseError)):
        merge_results(paths, tmp_path / "pytest.xml")
    assert not (tmp_path / "pytest.xml").exists()


@pytest.mark.parametrize("failure", ["call", "setup", "collection"])
def test_phase_merge_preserves_real_pytest_error_reports(
    tmp_path: Path, failure: str
) -> None:
    """Keep pytest's duplicate identities, multiple errors and collection diagnostics."""
    from scripts.run_wx_tests import merge_results

    module = tmp_path / "test_errors.py"
    module.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def finish():\n"
        " yield\n"
        " raise RuntimeError('teardown failure')\n"
        "@pytest.fixture\n"
        "def ready(finish):\n"
        f" if {failure == 'setup'}: raise RuntimeError('setup failure')\n"
        "def test_error(ready): assert False, 'call failure'\n"
        + (
            "raise RuntimeError('collection failure')\n"
            if failure == "collection"
            else ""
        )
    )
    raw = tmp_path / "raw.xml"
    environment = dict(
        os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS=""
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(module), f"--junitxml={raw}"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == (2 if failure == "collection" else 1), (
        result.stdout + result.stderr
    )
    merged = tmp_path / "merged.xml"
    assert merge_results([raw], merged)
    original = list(ET.parse(raw).iter("testcase"))  # noqa: S314 - owned pytest output
    assert [ET.tostring(case) for case in ET.parse(merged).iter("testcase")] == [  # noqa: S314 - owned pytest output
        ET.tostring(case) for case in original
    ]
