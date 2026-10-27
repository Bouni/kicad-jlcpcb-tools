"""Audit complete KiCad coverage while overlapping independent functional work."""

from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Optional
from xml.etree import ElementTree as ET

import pytest

from scripts import native_kicad_runner as runner


def _xml(path: Path, nodes: list[str], *, failed: bool = False) -> None:
    """Retain ordinary pytest case identity and native timing properties."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests=str(len(nodes)), time="1.5")
    for node in nodes:
        filename, name = node.split("::", 1)
        case = ET.SubElement(
            suite, "testcase", classname=filename[:-3].replace("/", "."), name=name
        )
        props = ET.SubElement(case, "properties")
        ET.SubElement(props, "property", name=runner.NODEID_PROPERTY, value=node)
        ET.SubElement(props, "property", name="native_performance_samples", value="raw")
        if failed:
            ET.SubElement(case, "failure", message="native failure")
    path.write_bytes(ET.tostring(root))


def test_partition_preserves_every_native_case_and_budget_samples() -> None:
    """New functional cases cannot disappear from the native inventory."""
    selected = [*runner.REQUIRED_BUDGETS, "tests/test_new_native.py::test_new"]
    budgets, functional = runner.partition(selected)
    assert set(budgets) == set(runner.REQUIRED_BUDGETS)
    assert functional == [selected[-1]]
    assert set(budgets).isdisjoint(functional)
    assert set(budgets + functional) == set(selected)


def test_functional_workers_keep_modules_and_shared_rf_cohort_together() -> None:
    """Do not duplicate RF preparation or distribute a module's fixture across workers."""
    modules = {
        "tests/test_rf_impedance_native.py": 168,
        "tests/test_rf_impedance_fixture_realism.py": 16,
        "tests/test_other_native.py": 120,
        "tests/test_more_native.py": 76,
    }
    selected = [
        f"{module}::test_case[{index}]"
        for module, count in modules.items()
        for index in range(count)
    ]
    shards = runner.functional_shards(selected)
    assert len(shards) == 2
    assert set(shards[0]).isdisjoint(shards[1])
    assert set(shards[0] + shards[1]) == set(selected)
    owners = {}
    for index, shard in enumerate(shards):
        for node in shard:
            module = node.split("::", 1)[0]
            assert owners.setdefault(module, index) == index
    assert (
        owners["tests/test_rf_impedance_native.py"]
        == owners["tests/test_rf_impedance_fixture_realism.py"]
    )
    assert [len(shard) for shard in shards] == [184, 196]
    assert runner.functional_shards(selected) == shards


@pytest.mark.parametrize(
    "selected", ([], ["tests/only.py::test_one"], ["tests/a.py::test_a"] * 2)
)
def test_functional_workers_reject_empty_duplicate_or_unsplittable_inventory(
    selected: list[str],
) -> None:
    """Fail visibly rather than silently splitting a module or omitting a case."""
    with pytest.raises(runner.RunnerError):
        runner.functional_shards(selected)


@pytest.mark.parametrize("damage", ("missing-budget", "duplicate", "no-functional"))
def test_partition_rejects_incomplete_or_ambiguous_coverage(damage: str) -> None:
    """A partial collection cannot silently establish complete coverage."""
    selected = [*runner.REQUIRED_BUDGETS, "tests/test_new_native.py::test_new"]
    if damage == "missing-budget":
        selected.pop(0)
    elif damage == "duplicate":
        selected.append(selected[-1])
    else:
        selected.pop()
    with pytest.raises(runner.RunnerError):
        runner.partition(selected)


def test_junit_merge_preserves_case_properties_and_independent_suites(
    tmp_path: Path,
) -> None:
    """Combining phases must retain native measurements and standard case names."""
    nodes = ["tests/a.py::test_budget[draft]", "tests/b.py::test_functional"]
    paths = [tmp_path / "a.xml", tmp_path / "b.xml"]
    for path, node in zip(paths, nodes):
        _xml(path, [node])
    output = tmp_path / "pytest.xml"
    runner.merge_junit(paths, output, nodes)
    root = ET.parse(output).getroot()  # noqa: S314 - owned local test evidence
    assert len(root.findall("testsuite")) == 2
    assert [case.get("name") for case in root.iter("testcase")] == [
        "test_budget[draft]",
        "test_functional",
    ]
    assert [
        p.get("value")
        for p in root.iter("property")
        if p.get("name") == "native_performance_samples"
    ] == ["raw", "raw"]


@pytest.mark.parametrize("damage", ("missing", "duplicate", "failure", "counter"))
def test_junit_merge_rejects_partial_duplicate_or_failed_evidence(
    tmp_path: Path, damage: str
) -> None:
    """A valid-looking partial XML cannot make the combined lane green."""
    path, output = tmp_path / "raw.xml", tmp_path / "pytest.xml"
    nodes = ["tests/a.py::test_a", "tests/b.py::test_b"]
    _xml(path, nodes[:1] if damage == "missing" else nodes, failed=damage == "failure")
    if damage == "counter":
        path.write_text(path.read_text().replace('tests="2"', 'tests="9"'))
    with pytest.raises(runner.RunnerError):
        runner.merge_junit(
            [path, path] if damage == "duplicate" else [path], output, nodes
        )
    assert not output.exists()


def test_budget_completion_precedes_overlapping_functional_workers() -> None:
    """Both functional workers must actually overlap after the uncontended gate."""
    barrier = threading.Barrier(3)
    events = []

    def phase(name: str) -> dict:
        events.append(name + "-start")
        if name != "budgets":
            assert "budgets-end" in events
            barrier.wait(timeout=2)
        events.append(name + "-end")
        return {"name": name, "status": "PASS", "seconds": 0.01}

    result = runner.schedule(phase)
    assert events[:2] == ["budgets-start", "budgets-end"]
    assert [item["name"] for item in result] == [
        "budgets",
        "functional-0",
        "functional-1",
        "smoke",
    ]


def test_budget_failure_never_launches_functional_work() -> None:
    """Failed timing gates retain isolation and fail without launching siblings."""
    calls = []

    def phase(name: str) -> dict:
        calls.append(name)
        raise runner.RunnerError("budget failed")

    with pytest.raises(runner.RunnerError, match="budget failed"):
        runner.schedule(phase)
    assert calls == ["budgets"]


def test_failed_functional_worker_cancels_and_joins_its_sibling() -> None:
    """Failure cleanup is completed before schedule returns to remove private state."""
    cancelled = threading.Event()
    joined = threading.Event()

    def phase(name: str) -> dict:
        if name == "functional-0":
            raise runner.RunnerError("functional failed")
        if name == "smoke":
            assert cancelled.wait(2)
            joined.set()
        return {"name": name, "status": "PASS", "seconds": 0.01}

    with pytest.raises(runner.RunnerError, match="functional failed"):
        runner.schedule(phase, cancel=cancelled.set)
    assert joined.is_set()


def test_concurrent_cleanup_sends_only_one_termination_before_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation and a worker's finally must not reenter native teardown handlers."""
    signals = []
    start = threading.Barrier(2)

    def killpg(pid: int, number: int) -> None:
        assert pid == 987654321
        signals.append(number)
        if number == signal.SIGTERM:
            time.sleep(0.03)

    class Process:
        pid = 987654321

        def wait(self, timeout: float = 0) -> int:
            return 0

    monkeypatch.setattr(os, "killpg", killpg)
    supervisor = runner.ProcessSupervisor()
    process = Process()

    def stop() -> None:
        start.wait(timeout=1)
        supervisor._stop(process)

    threads = [threading.Thread(target=stop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert signals.count(signal.SIGTERM) == 1


def test_cleanup_retry_and_process_identity_require_completed_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed reap retries, and a later process with the same PID is independently owned."""
    attempts = []

    class Process:
        pid = 987654321

    def stop(process: Process) -> None:
        attempts.append(process)
        if len(attempts) == 1:
            raise RuntimeError("reap interrupted")

    monkeypatch.setattr(
        runner.ProcessSupervisor, "_terminate_group", staticmethod(stop)
    )
    supervisor = runner.ProcessSupervisor()
    first, later = Process(), Process()
    with pytest.raises(RuntimeError, match="reap interrupted"):
        supervisor._stop(first)
    supervisor._stop(first)
    supervisor._stop(first)
    supervisor._stop(later)
    assert attempts == [first, first, later]


def test_private_preferences_preserve_seed_without_sharing_mutable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private native worker retains the configured default palette and isolation."""
    seed = tmp_path / "seed"
    (seed / "10.0").mkdir(parents=True)
    (seed / "10.0" / "pcbnew.json").write_text(
        '{"appearance":{"color_theme":"_builtin_default"}}'
    )
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(seed))
    monkeypatch.setenv("KICAD_JLCPCB_NATIVE_TESTS", "0")
    monkeypatch.setenv("KICAD_JLCPCB_REQUIRE_NATIVE", "wx")
    monkeypatch.setenv("KICAD_JLCPCB_KICAD_MAJOR", "7")
    with runner.native_environment(tmp_path) as env:
        assert env["KICAD_JLCPCB_NATIVE_TESTS"] == "1"
        assert env["KICAD_JLCPCB_REQUIRE_NATIVE"] == "kicad"
        assert env["KICAD_JLCPCB_KICAD_MAJOR"] == "10"
        private = Path(env["KICAD_CONFIG_HOME"])
        assert private != seed
        assert (private / "10.0" / "pcbnew.json").read_bytes() == (
            seed / "10.0" / "pcbnew.json"
        ).read_bytes()
        (private / "10.0" / "pcbnew.json").write_text("private mutation")
        assert "_builtin_default" in (seed / "10.0" / "pcbnew.json").read_text()
        assert env["TMPDIR"] != str(seed)
    assert os.environ["KICAD_JLCPCB_NATIVE_TESTS"] == "0"
    assert os.environ["KICAD_JLCPCB_REQUIRE_NATIVE"] == "wx"
    assert os.environ["KICAD_JLCPCB_KICAD_MAJOR"] == "7"


@pytest.mark.parametrize("mode", ("collection", "budgets", "functional"))
def test_each_pytest_phase_keeps_strict_kicad_flags_and_exact_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Fresh native children inherit strict runtime checks, without a second selector."""
    node = "tests/example.py::test_native"
    item = SimpleNamespace(nodeid=node, user_properties=[])
    seen = []

    def pytest_main(options: list[str], plugins: list) -> int:
        seen.extend(options)
        plugins[0].pytest_collection_finish(SimpleNamespace(items=[item]))
        return 0

    monkeypatch.setattr(pytest, "main", pytest_main)
    arguments = ["--mode", mode, "--output", str(tmp_path)]
    if mode == "functional":
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"selected": [node]}))
        arguments += ["--manifest", str(manifest)]
    assert runner._pytest(arguments) == 0
    assert "--require-native=kicad" in seen
    assert "--kicad-major=10" in seen
    assert seen[seen.index("-m") + 1] == "native_kicad"
    assert item.user_properties == [(runner.NODEID_PROPERTY, node)]
    assert (runner.BUDGET_MODULE in seen) == (mode == "budgets")
    assert ("--ignore=" + runner.BUDGET_MODULE in seen) == (mode == "functional")
    assert ("--collect-only" in seen) == (mode == "collection")


@pytest.mark.parametrize(
    "actual", ([], ["tests/example.py::test_native", "tests/example.py::test_new"])
)
def test_scoped_functional_collection_rejects_missing_or_new_unassigned_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actual: list[str]
) -> None:
    """A worker cannot silently omit assigned cases or run cases absent from its audit."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"selected": ["tests/example.py::test_native"]}))

    def pytest_main(options: list[str], plugins: list) -> int:
        assert "tests/example.py" in options
        plugins[0].pytest_collection_finish(
            SimpleNamespace(
                items=[
                    SimpleNamespace(nodeid=node, user_properties=[]) for node in actual
                ]
            )
        )
        return 0

    monkeypatch.setattr(pytest, "main", pytest_main)
    with pytest.raises(pytest.UsageError, match="differs from worker manifest"):
        runner._pytest(
            [
                "--mode",
                "functional",
                "--output",
                str(tmp_path),
                "--manifest",
                str(manifest),
            ]
        )


@pytest.fixture
def tiny_native_workflow(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Use real pytest processes with explicitly labeled display/KiCad stand-ins."""
    suite = tmp_path / "suite"
    (suite / "tests").mkdir(parents=True)
    (suite / "scripts").mkdir()
    proof = tmp_path / "proof"
    proof.mkdir()
    (suite / "pytest.ini").write_text(
        "[pytest]\ntestpaths=tests\nmarkers=\n native_kicad: labeled native stand-in\n"
    )
    (suite / "conftest.py").write_text(
        "import os\n"
        "def pytest_addoption(parser):\n"
        " parser.addoption('--require-native')\n"
        " parser.addoption('--kicad-major')\n"
        "def pytest_sessionstart(session):\n"
        " assert session.config.getoption('require_native')=='kicad'\n"
        " assert session.config.getoption('kicad_major')=='10'\n"
        " assert os.environ['DISPLAY'].startswith(':')\n"
        " os.environ['KICAD_JLCPCB_REQUIRE_NATIVE']='kicad'\n"
        " os.environ['KICAD_JLCPCB_KICAD_MAJOR']='10'\n"
    )
    (suite / runner.BUDGET_MODULE).write_text(
        "import os,pathlib,pytest\n"
        "pytestmark=pytest.mark.native_kicad\n"
        "@pytest.mark.parametrize('approved',[False,True],ids=['draft','approved'])\n"
        "def test_native_kicad_configure_and_save_budgets(approved,record_property):\n"
        " pathlib.Path(os.environ['TINY_PROOF'],'budget-'+str(approved)).touch()\n"
        " record_property('native_performance_samples',{'cold':True,'seconds':0.01})\n"
        "def test_native_kicad_first_use_configure_budgets(record_property):\n"
        " pathlib.Path(os.environ['TINY_PROOF'],'budget-first').touch()\n"
        " record_property('native_performance_samples',{'samples':[1,2,3]})\n"
    )
    (suite / "tests/test_new_native.py").write_text(
        "import json,os,pathlib,time,pytest\n"
        "@pytest.mark.native_kicad\n"
        "def test_functional():\n"
        " proof=pathlib.Path(os.environ['TINY_PROOF'])\n"
        " assert len(list(proof.glob('budget-*')))==3\n"
        " time.sleep(float(os.environ.get('TINY_FUNCTIONAL_STARTUP_DELAY','0')))\n"
        " details=json.dumps({'pid':os.getpid(),'config':os.environ['KICAD_CONFIG_HOME']})\n"
        " (proof/'functional-started.json').write_text(details)\n"
        " if os.environ.get('TINY_OMIT_READY')!=pathlib.Path(__file__).name: (proof/'functional.json').write_text(details)\n"
        " deadline=time.monotonic()+5\n"
        " while not (proof/'smoke.json').exists():\n"
        "  assert time.monotonic()<deadline\n"
        "  time.sleep(0.01)\n"
        " if os.environ.get('TINY_PRIMARY_FAILURE')=='1' and pathlib.Path(__file__).name=='test_new_native.py':\n"
        "  while not all((proof/name).exists() for name in ('functional-second.json','detached.pid')):\n"
        "   assert time.monotonic()<deadline\n"
        "   time.sleep(0.01)\n"
        "  raise AssertionError('primary native failure')\n"
        " if os.environ.get('TINY_MODE')!='success': time.sleep(30)\n"
    )
    (suite / "tests/test_second_native.py").write_text(
        (suite / "tests/test_new_native.py")
        .read_text()
        .replace("'functional.json'", "'functional-second.json'")
        .replace("'functional-started.json'", "'functional-second-started.json'")
    )
    (suite / "scripts/linux_native_impedance_smoke.py").write_text(
        "import argparse,json,os,pathlib,signal,subprocess,sys,time\n"
        "p=argparse.ArgumentParser();p.add_argument('--output');p.add_argument('--timeout');p.add_argument('--workers');a=p.parse_args()\n"
        "assert a.workers=='2'\n"
        "proof=pathlib.Path(os.environ['TINY_PROOF']);assert len(list(proof.glob('budget-*')))==3\n"
        "output=pathlib.Path(a.output);output.mkdir()\n"
        "child=None\n"
        "def terminate(*args):\n"
        " if child is not None:\n"
        "  os.killpg(child.pid,signal.SIGTERM);child.wait(timeout=2)\n"
        " (proof/'smoke-cleanup.json').write_text(json.dumps({'config_exists':pathlib.Path(os.environ['KICAD_CONFIG_HOME']).is_dir()}))\n"
        " sys.exit(1)\n"
        "signal.signal(signal.SIGTERM,terminate)\n"
        "if os.environ.get('TINY_MODE')!='success':\n"
        " code=\"import os,pathlib,time; p=pathlib.Path(os.environ['TINY_PROOF']);(p/'detached.pid').write_text(str(os.getpid()));f=(p/'heartbeat').open('ab',buffering=0);exec('while True: f.write(b\\\"x\\\");time.sleep(0.02)')\"\n"
        " child=subprocess.Popen([sys.executable,'-c',code],start_new_session=True)\n"
        "(proof/'smoke.json').write_text(json.dumps({'pid':os.getpid(),'config':os.environ['KICAD_CONFIG_HOME']}))\n"
        "deadline=time.monotonic()+5\n"
        "while not (proof/'functional.json').exists() or not (proof/'functional-second.json').exists():\n"
        " assert time.monotonic()<deadline\n"
        " time.sleep(0.01)\n"
        "if child is not None: time.sleep(30)\n"
        "stages=['prerequisites','single-ended-50-ohm','usb-differential-90-ohm','custom-palette','dialogs','workflow','generate']\n"
        "(output/'summary.json').write_text(json.dumps({'status':'PASS','stages':[{'stage':s,'status':'PASS'} for s in stages]}))\n"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    wrapper = binaries / "xvfb-run"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        "assert sys.argv[1:4]==['-a','-s','-screen 0 1920x1080x24 -noreset'], sys.argv\n"
        "os.environ['DISPLAY']=':'+str(os.getpid())\nos.execv(sys.argv[4],sys.argv[4:])\n"
    )
    wrapper.chmod(0o755)
    return (
        suite,
        proof,
        dict(
            os.environ,
            PATH=str(binaries) + os.pathsep + os.environ["PATH"],
            TINY_PROOF=str(proof),
            TINY_MODE="success",
        ),
    )


def _tiny_command(suite: Path, output: Path, timeout: float) -> list[str]:
    """Exercise the real public main function with only the fixture repository changed."""
    return [
        sys.executable,
        "-c",
        "import sys;from pathlib import Path;"
        f"sys.path.insert(0,{str(Path(runner.__file__).resolve().parents[1])!r});"
        "from scripts import native_kicad_runner as r;"
        f"r.ROOT=Path({str(suite)!r});"
        f"raise SystemExit(r.main(['--artifacts',{str(output)!r},'--timeout-seconds',{str(timeout)!r}]))",
    ]


def _run_ready_cancellation(suite: str, output: str) -> int:
    """Arm the real cleanup timeout only after real fixture processes are active."""
    from scripts import native_impedance_support

    real_popen = subprocess.Popen
    proof = Path(os.environ["TINY_PROOF"])

    class ReadyProcess(real_popen):
        """Keep real process supervision while separating startup and cleanup clocks."""

        def wait(self, timeout: Optional[float] = None) -> int:
            command = self.args
            functional = isinstance(command, list) and "functional" in command
            if functional and timeout is not None and not getattr(self, "armed", False):
                self.armed = True
                # Each process is already registered with the real supervisor.
                # Leave its original stop/reap waits available during readiness.
                required = (
                    "functional-started.json",
                    "functional-second-started.json",
                    "smoke.json",
                    "detached.pid",
                )
                deadline = time.monotonic() + 10
                while not all((proof / name).exists() for name in required):
                    if self.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Fixture startup readiness failed")
                    time.sleep(0.01)
                deadline = time.monotonic() + 1.5
                while not all(
                    (proof / name).exists()
                    for name in ("functional.json", "functional-second.json")
                ):
                    if self.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Fixture functional readiness failed")
                    time.sleep(0.01)
                (proof / f"timeout-armed-{self.pid}").touch()
                deadline = time.monotonic() + 10
                while len(list(proof.glob("timeout-armed-*"))) != 2:
                    if self.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Fixture timeout arming failed")
                    time.sleep(0.01)
                timeout = 1.5
            return super().wait(timeout=timeout)

    runner.ROOT = Path(suite)
    native_impedance_support.subprocess.Popen = ReadyProcess
    try:
        return runner.main(["--artifacts", output, "--timeout-seconds", "10"])
    finally:
        native_impedance_support.subprocess.Popen = real_popen


def _ready_cancellation_command(suite: Path, output: Path) -> list[str]:
    """Import the test-only readiness shim inside the actual runner subprocess."""
    return [
        sys.executable,
        "-c",
        "import sys;"
        f"sys.path.insert(0,{str(Path(runner.__file__).resolve().parents[1])!r});"
        "from tests.test_native_kicad_runner import _run_ready_cancellation;"
        f"raise SystemExit(_run_ready_cancellation({str(suite)!r},{str(output)!r}))",
    ]


def _run_signal_during_cleanup(suite: str, output: str) -> int:
    """Send a real signal at the first owned cleanup boundary of the actual runner."""
    real_supervisor = runner.ProcessSupervisor
    artifacts = Path(output)

    class SignalledCleanup(real_supervisor):
        """Keep actual processes, group termination and reaping behind the signal boundary."""

        injected = False

        def stop_all(self) -> None:
            if not self.injected:
                self.injected = True
                (artifacts / "cleanup-entered").touch()
                os.kill(os.getpid(), signal.SIGTERM)
            super().stop_all()

    previous = {
        number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)
    }
    runner.ROOT = Path(suite)
    runner.ProcessSupervisor = SignalledCleanup
    try:
        return runner.run(artifacts, 10, 30)
    finally:
        runner.write_json(
            artifacts / "handler-restoration.json",
            {
                "restored": all(
                    signal.getsignal(number) == handler
                    for number, handler in previous.items()
                )
            },
        )
        runner.ProcessSupervisor = real_supervisor


@pytest.mark.parametrize("cause", ["cancelled", "worker-failed"])
def test_signal_during_cleanup_preserves_failure_and_reaps_all_workers(
    tiny_native_workflow: tuple[Path, Path, dict[str, str]], tmp_path: Path, cause: str
) -> None:
    """Repeated cancellation cannot interrupt real cleanup or replace a worker's first failure."""
    suite, proof, environment = tiny_native_workflow
    environment["TINY_MODE"] = "cleanup-signal"
    if cause == "worker-failed":
        environment["TINY_PRIMARY_FAILURE"] = "1"
    output = tmp_path / "artifacts"
    command = [
        sys.executable,
        "-c",
        "import sys;"
        f"sys.path.insert(0,{str(Path(runner.__file__).resolve().parents[1])!r});"
        "from tests.test_native_kicad_runner import _run_signal_during_cleanup;"
        f"raise SystemExit(_run_signal_during_cleanup({str(suite)!r},{str(output)!r}))",
    ]
    workers = {
        "functional-started.json": "pid",
        "functional-second-started.json": "pid",
        "smoke.json": "pid",
        "detached.pid": None,
    }
    with (tmp_path / "parent.log").open("w") as log:
        parent = subprocess.Popen(
            command,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not all((proof / filename).exists() for filename in workers):
                assert parent.poll() is None, (tmp_path / "parent.log").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if cause == "cancelled":
                parent.send_signal(signal.SIGTERM)
            assert parent.wait(timeout=3) == 1
        finally:
            if parent.poll() is None:
                for filename, key in workers.items():
                    if (proof / filename).exists():
                        contents = (proof / filename).read_text()
                        pid = (
                            int(contents) if key is None else json.loads(contents)[key]
                        )
                        with suppress(ProcessLookupError):
                            os.killpg(pid, signal.SIGKILL)
                parent.kill()
                parent.wait(timeout=3)
    assert (output / "cleanup-entered").is_file()
    for filename, key in workers.items():
        contents = (proof / filename).read_text()
        pid = int(contents) if key is None else json.loads(contents)[key]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    orchestration = output / "orchestration"
    summary = json.loads((orchestration / "summary.json").read_text())
    assert summary["status"] == "FAIL"
    expected_error = (
        "Native KiCad orchestration cancelled"
        if cause == "cancelled"
        else "Native functional-0 failed:"
    )
    assert summary["error"].startswith(expected_error)
    if cause == "worker-failed":
        assert (
            "primary native failure"
            in (orchestration / "functional-0/run.log").read_text()
        )
    assert all(
        (orchestration / name / "phase.json").is_file()
        for name in ("functional-0", "functional-1", "smoke")
    )
    assert json.loads((output / "handler-restoration.json").read_text()) == {
        "restored": True
    }
    assert not (output / "pytest.xml").exists()
    assert not list(orchestration.glob("native-wx-*"))


def test_real_tiny_workflow_collects_budgets_overlaps_and_merges(
    tiny_native_workflow: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """Run actual subprocess pytest, supervision, private environments and JUnit merge."""
    suite, proof, env = tiny_native_workflow
    output = tmp_path / "artifacts"
    completed = subprocess.run(
        _tiny_command(suite, output, 5),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((output / "orchestration/summary.json").read_text())
    assert summary["status"] == "PASS"
    assert (summary["budget_tests"], summary["functional_tests"]) == (3, 2)
    assert summary["functional_workers"] == [1, 1]
    root = ET.parse(output / "pytest.xml").getroot()  # noqa: S314 - owned fixture evidence
    assert len(list(root.iter("testcase"))) == 5
    assert (
        len(
            [
                p
                for p in root.iter("property")
                if p.get("name") == "native_performance_samples"
            ]
        )
        == 3
    )
    configs = {
        json.loads((proof / (name + ".json")).read_text())["config"]
        for name in ("functional", "functional-second", "smoke")
    }
    assert len(configs) == 3
    assert all(not Path(path).exists() for path in configs)
    assert not list((output / "orchestration").glob("native-wx-*"))


def test_scoped_native_worker_opts_in_before_initial_nested_conftest_import(
    tiny_native_workflow: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """Explicit module paths must not freeze a native opt-in marker before sessionstart."""
    suite, _proof, env = tiny_native_workflow
    env.pop("KICAD_JLCPCB_NATIVE_TESTS", None)
    conftest = suite / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + " os.environ['KICAD_JLCPCB_NATIVE_TESTS']='1'\n"
        + "import pytest\n"
        + "@pytest.hookimpl(hookwrapper=True)\n"
        + "def pytest_runtest_makereport(item,call):\n"
        + " report=(yield).get_result()\n"
        + " if report.skipped and item.get_closest_marker('native_kicad'):\n"
        + "  report.outcome='failed';report.longrepr='Required native test skipped: early opt-in frozen'\n"
    )
    (suite / "native_plot_gate_for_runner_proof.py").write_text(
        "import os,pytest\n"
        "requires_native_plotting=pytest.mark.skipif(os.environ.get('KICAD_JLCPCB_NATIVE_TESTS')!='1',reason='native plotting requires explicitly enabled desktop session')\n"
    )
    backend = suite / "tests/backend_contract"
    backend.mkdir()
    (backend / "conftest.py").write_text(
        "from native_plot_gate_for_runner_proof import requires_native_plotting\n"
    )
    (backend / "test_reads.py").write_text(
        "import pytest\n"
        "from native_plot_gate_for_runner_proof import requires_native_plotting\n"
        "@pytest.mark.native_kicad\n"
        "@requires_native_plotting\n"
        "def test_native_read(): pass\n"
    )
    output = tmp_path / "artifacts"
    completed = subprocess.run(
        _tiny_command(suite, output, 5),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    root = ET.parse(output / "pytest.xml").getroot()  # noqa: S314 - owned fixture evidence
    assert len(list(root.iter("testcase"))) == 6
    assert not list(root.iter("skipped"))


@pytest.mark.parametrize("ending", ("sigterm", "timeout", "readiness-failure"))
def test_real_parent_cancellation_reaps_detached_smoke_before_private_cleanup(
    tiny_native_workflow: tuple[Path, Path, dict[str, str]], tmp_path: Path, ending: str
) -> None:
    """Do not leave detached native stage sessions or publish partial green evidence."""
    suite, proof, env = tiny_native_workflow
    env["TINY_MODE"] = ending
    if ending == "timeout":
        env["TINY_FUNCTIONAL_STARTUP_DELAY"] = "1.75"
    if ending == "readiness-failure":
        env["TINY_OMIT_READY"] = "test_second_native.py"
    output = tmp_path / "artifacts"
    with (tmp_path / "parent.log").open("w") as log:
        parent = subprocess.Popen(
            _tiny_command(suite, output, 10)
            if ending == "sigterm"
            else _ready_cancellation_command(suite, output),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            required = [
                "detached.pid",
                "smoke.json",
                "functional-started.json",
                "functional-second-started.json",
            ]
            if ending != "readiness-failure":
                required += ["functional.json", "functional-second.json"]
            while not all((proof / name).exists() for name in required):
                assert parent.poll() is None, (tmp_path / "parent.log").read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if ending == "sigterm":
                parent.send_signal(signal.SIGTERM)
            assert parent.wait(timeout=7) != 0
        finally:
            if parent.poll() is None:
                parent.send_signal(signal.SIGTERM)
                try:
                    parent.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    parent.kill()
                    parent.wait()
    for filename, key in (
        ("detached.pid", None),
        ("functional-started.json", "pid"),
        ("functional-second-started.json", "pid"),
        ("smoke.json", "pid"),
    ):
        contents = (proof / filename).read_text()
        pid = int(contents) if key is None else json.loads(contents)[key]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert json.loads((proof / "smoke-cleanup.json").read_text())["config_exists"]
    summary = json.loads((output / "orchestration/summary.json").read_text())
    assert summary["status"] == "FAIL"
    if ending == "timeout":
        assert len(list(proof.glob("timeout-armed-*"))) == 2
        assert any(
            phase.get("timed_out")
            for phase in summary["phases"]
            if phase["name"].startswith("functional-")
        )
    if ending == "readiness-failure":
        assert "Fixture functional readiness failed" in summary["error"]
        assert not list(proof.glob("timeout-armed-*"))
    assert not (output / "pytest.xml").exists()
    assert not list((output / "orchestration").glob("native-wx-*"))
