"""Keep native timing gates isolated, then overlap complete functional workflows."""

import argparse
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from typing import Any, Optional
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_impedance_support import (  # noqa: E402
    ProcessSupervisor,
    private_environment,
    write_json,
)

NODEID_PROPERTY = "native_kicad_runner_nodeid"
BUDGET_MODULE = "tests/test_impedance_performance_native.py"
REQUIRED_BUDGETS = (
    BUDGET_MODULE + "::test_native_kicad_configure_and_save_budgets[draft]",
    BUDGET_MODULE + "::test_native_kicad_configure_and_save_budgets[approved]",
    BUDGET_MODULE + "::test_native_kicad_first_use_configure_budgets",
)


class RunnerError(Exception):
    """Reject incomplete native execution or inconsistent coverage evidence."""


def partition(selected: list[str]) -> tuple[list[str], list[str]]:
    """Keep every current native case, including newly introduced budget cases."""
    if len(set(selected)) != len(selected) or not set(REQUIRED_BUDGETS) <= set(
        selected
    ):
        raise RunnerError("Duplicate native cases or missing required timing gates")
    budgets = [node for node in selected if node.split("::", 1)[0] == BUDGET_MODULE]
    functional = [node for node in selected if node.split("::", 1)[0] != BUDGET_MODULE]
    if not budgets or not functional:
        raise RunnerError("Both native timing and functional inventories are required")
    return budgets, functional


def functional_shards(selected: list[str]) -> list[list[str]]:
    """Balance complete modules while keeping the shared native RF cache in one process."""
    if not selected or len(set(selected)) != len(selected):
        raise RunnerError("Empty or duplicate native functional inventory")
    rf_cohort = {
        "tests/test_rf_impedance_native.py",
        "tests/test_rf_impedance_fixture_realism.py",
    }
    groups: dict[str, list[str]] = {}
    for node in selected:
        module = node.split("::", 1)[0]
        key = "RF preparation cohort" if module in rf_cohort else module
        groups.setdefault(key, []).append(node)
    if len(groups) < 2:
        raise RunnerError("Two complete native functional module groups are required")
    shards: list[list[str]] = [[], []]
    for key in sorted(groups, key=lambda key: (-len(groups[key]), key)):
        target = min(range(2), key=lambda index: (len(shards[index]), index))
        shards[target].extend(groups[key])
    positions = {node: index for index, node in enumerate(selected)}
    for shard in shards:
        shard.sort(key=positions.__getitem__)
    if any(not shard for shard in shards) or Counter(shards[0] + shards[1]) != Counter(
        selected
    ):
        raise RunnerError("Native functional worker union differs from collection")
    return shards


def merge_junit(paths: list[Path], output: Path, selected: list[str]) -> None:
    """Retain complete original suites, native properties and standard case names."""
    combined = ET.Element("testsuites")
    identities = []
    legacy = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()  # noqa: S314 - owned local pytest evidence
        except (OSError, ET.ParseError) as error:
            raise RunnerError(f"Missing or invalid JUnit: {path}") from error
        if root.tag not in {"testsuite", "testsuites"}:
            raise RunnerError(f"Invalid JUnit root: {path}")
        suites = [root] if root.tag == "testsuite" else list(root)
        if not suites or any(suite.tag != "testsuite" for suite in suites):
            raise RunnerError(f"Empty or invalid JUnit suites: {path}")
        for suite in suites:
            cases = list(suite.iter("testcase"))
            try:
                if int(suite.get("tests", "-1")) != len(cases):
                    raise RunnerError(f"Inconsistent JUnit count: {path}")
                if int(suite.get("failures", "0")) or int(suite.get("errors", "0")):
                    raise RunnerError(f"Failed JUnit suite: {path}")
            except ValueError as error:
                raise RunnerError(f"Invalid JUnit counters: {path}") from error
            for case in cases:
                if case.find("failure") is not None or case.find("error") is not None:
                    raise RunnerError(f"Failed native JUnit case: {path}")
                properties = [
                    item.get("value")
                    for item in case.findall("./properties/property")
                    if item.get("name") == NODEID_PROPERTY
                ]
                if len(properties) != 1:
                    raise RunnerError(f"Missing exact native node identity: {path}")
                identities.extend(properties)
                legacy.append((case.get("classname"), case.get("name")))
            combined.append(suite)
    if Counter(identities) != Counter(selected) or len(set(legacy)) != len(legacy):
        raise RunnerError("Combined JUnit differs from complete native collection")
    temporary = output.with_suffix(".xml.tmp")
    ET.ElementTree(combined).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(output)


@contextmanager
def native_environment(parent: Path) -> Iterator[dict[str, str]]:
    """Copy native palette preferences into independently owned private state."""
    configured = os.environ.get("KICAD_CONFIG_HOME")
    with private_environment(parent) as env:
        # Explicit module paths load their nested conftests before sessionstart.
        # Enable the promised desktop profile before any native support import;
        # pytest's strict preflight still verifies the real bindings and major.
        env.update(
            KICAD_JLCPCB_NATIVE_TESTS="1",
            KICAD_JLCPCB_REQUIRE_NATIVE="kicad",
            KICAD_JLCPCB_KICAD_MAJOR="10",
        )
        destination = Path(env["KICAD_CONFIG_HOME"])
        if configured:
            source = Path(configured)
            if source.is_symlink() or any(
                path.is_symlink() for path in source.rglob("*")
            ):
                raise RunnerError("Native seed preferences must not contain symlinks")
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
        version = destination / "10.0"
        version.mkdir(exist_ok=True)
        preferences = version / "pcbnew.json"
        if not preferences.exists():
            preferences.write_text(
                '{"appearance":{"color_theme":"_builtin_default"}}\n'
            )
        yield env


def schedule(
    phase: Callable[[str], dict[str, Any]],
    *,
    cancel: Callable[[], None] = lambda: None,
) -> list[dict[str, Any]]:
    """Complete native budget gates before launching both pytest workers and smoke."""
    results = [phase("budgets")]
    names = ("functional-0", "functional-1", "smoke")
    with ThreadPoolExecutor(max_workers=3) as executor:
        active = {executor.submit(phase, name): name for name in names}
        completed = {}
        try:
            while active:
                ready, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in ready:
                    name = active.pop(future)
                    completed[name] = future.result()
        except BaseException:
            cancel()
            raise
    return results + [completed[name] for name in names]


def _pytest(argv: list[str]) -> int:
    """Run ordinary strict pytest without passing selectors into native children."""
    import pytest

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("collection", "budgets", "functional"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    selected = []
    expected = (
        json.loads(args.manifest.read_text())["selected"] if args.manifest else None
    )
    if args.mode == "functional" and not expected:
        parser.error("Functional native workers require an explicit nonempty manifest")

    class Inventory:
        """Attach exact identities while preserving existing native skip policy."""

        def pytest_collection_finish(self, session: Any) -> None:
            for item in session.items:
                selected.append(item.nodeid)
                item.user_properties.append((NODEID_PROPERTY, item.nodeid))
            if expected is not None and Counter(selected) != Counter(expected):
                raise pytest.UsageError(
                    "Scoped native collection differs from worker manifest"
                )

    options = [
        "-rs",
        "--require-native=kicad",
        "--kicad-major=10",
        "-m",
        "native_kicad",
        "--basetemp",
        str(args.output / "pytest-work"),
    ]
    if args.mode == "collection":
        options += ["--collect-only", "-q"]
    else:
        options += ["--junitxml=" + str(args.output / "pytest.xml")]
        options += (
            [BUDGET_MODULE] if args.mode == "budgets" else ["--ignore=" + BUDGET_MODULE]
        )
        if expected is not None:
            options += sorted({node.split("::", 1)[0] for node in expected})
    exitcode = int(pytest.main(options, plugins=[Inventory()]))
    write_json(
        args.output / "inventory.json", {"selected": selected, "exitcode": exitcode}
    )
    return exitcode


def run(artifacts: Path, timeout: float, smoke_timeout: int) -> int:
    """Retain complete raw evidence and publish PASS only after both workflows finish."""
    artifacts = artifacts.resolve()
    orchestration = artifacts / "orchestration"
    orchestration.mkdir(parents=True, exist_ok=False)
    if (artifacts / "pytest.xml").exists() or (artifacts / "impedance").exists():
        raise RunnerError("Native orchestration outputs must be fresh")
    started = time.monotonic()
    summary: dict[str, Any] = {"status": "FAIL", "phases": []}
    selected: list[str] = []
    expected: dict[str, list[str]] = {}
    previous = {
        number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)
    }
    cancelled = False

    def terminate(_number: int, _frame: Any) -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            raise KeyboardInterrupt("Native KiCad orchestration cancelled")

    for number in previous:
        signal.signal(number, terminate)
    try:
        with ProcessSupervisor() as supervisor:

            def cancel() -> None:
                nonlocal cancelled
                cancelled = True
                supervisor.stop_all()

            def phase(name: str) -> dict[str, Any]:
                directory = orchestration / name
                directory.mkdir()
                record: dict[str, Any] = {
                    "name": name,
                    "status": "FAIL",
                    "seconds": 0.0,
                }
                phase_started = time.monotonic()
                summary["phases"].append(record)
                try:
                    with native_environment(orchestration) as env:
                        if name == "smoke":
                            command = [
                                sys.executable,
                                str(ROOT / "scripts/linux_native_impedance_smoke.py"),
                                "--output",
                                str(artifacts / "impedance"),
                                "--timeout",
                                str(smoke_timeout),
                                "--workers",
                                "2",
                            ]
                            limit = 3000.0
                        else:
                            command = [
                                "xvfb-run",
                                "-a",
                                "-s",
                                "-screen 0 1920x1080x24 -noreset",
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "_pytest",
                                "--mode",
                                "functional"
                                if name.startswith("functional-")
                                else name,
                                "--output",
                                str(directory),
                            ]
                            if name.startswith("functional-"):
                                command += [
                                    "--manifest",
                                    str(orchestration / (name + "-manifest.json")),
                                ]
                            limit = timeout
                        outcome = supervisor.run(
                            command, ROOT, env, directory / "run.log", limit
                        )
                    record.update(outcome)
                    if outcome["timed_out"] or outcome["returncode"] != 0:
                        raise RunnerError(
                            f"Native {name} failed: {outcome}; see {directory / 'run.log'}"
                        )
                    if name != "smoke":
                        inventory = json.loads(
                            (directory / "inventory.json").read_text()
                        )
                        if inventory.get("exitcode") != 0 or not isinstance(
                            inventory.get("selected"), list
                        ):
                            raise RunnerError(f"Incomplete native inventory: {name}")
                        if name != "collection" and Counter(
                            inventory["selected"]
                        ) != Counter(expected[name]):
                            raise RunnerError(
                                f"Native {name} selection differs from full collection"
                            )
                    else:
                        evidence = json.loads(
                            (artifacts / "impedance/summary.json").read_text()
                        )
                        from scripts.linux_native_impedance_smoke import STAGES

                        stages = evidence.get("stages", [])
                        if (
                            evidence.get("status") != "PASS"
                            or [stage.get("stage") for stage in stages] != list(STAGES)
                            or any(stage.get("status") != "PASS" for stage in stages)
                        ):
                            raise RunnerError("Incomplete complete-smoke evidence")
                    record["status"] = "PASS"
                    return record
                except BaseException as error:
                    record["error"] = str(error)
                    raise
                finally:
                    record["seconds"] = time.monotonic() - phase_started
                    write_json(directory / "phase.json", record)

            phase("collection")
            selected = json.loads(
                (orchestration / "collection/inventory.json").read_text()
            )["selected"]
            expected["budgets"], expected["functional"] = partition(selected)
            for index, shard in enumerate(functional_shards(expected["functional"])):
                name = f"functional-{index}"
                expected[name] = shard
                write_json(
                    orchestration / (name + "-manifest.json"), {"selected": shard}
                )
            write_json(
                orchestration / "selected-manifest.json",
                {"selected": selected, **expected},
            )
            schedule(phase, cancel=cancel)
            merge_junit(
                [
                    orchestration / name / "pytest.xml"
                    for name in ("budgets", "functional-0", "functional-1")
                ],
                artifacts / "pytest.xml",
                selected,
            )
            summary.update(
                status="PASS",
                selected_tests=len(selected),
                budget_tests=len(expected["budgets"]),
                functional_tests=len(expected["functional"]),
                functional_workers=[
                    len(expected[f"functional-{index}"]) for index in range(2)
                ],
            )
        return 0
    except (Exception, KeyboardInterrupt) as error:
        summary["error"] = str(error)
        sys.stderr.write(str(error) + "\n")
        return 1
    finally:
        phase_order = ("collection", "budgets", "functional-0", "functional-1", "smoke")
        summary["phases"].sort(key=lambda phase: phase_order.index(phase["name"]))
        summary["elapsed_seconds"] = time.monotonic() - started
        summary["summed_worker_seconds"] = sum(
            phase["seconds"] for phase in summary["phases"]
        )
        write_json(orchestration / "summary.json", summary)
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: Optional[list[str]] = None) -> int:
    """Enter the strict KiCad 10 workflow or an exact internal pytest phase."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "_pytest":
        return _pytest(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--smoke-timeout-seconds", type=int, default=1800)
    args = parser.parse_args(arguments)
    if (
        not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
        or not 0 < args.smoke_timeout_seconds <= 3600
    ):
        parser.error(
            "Native timeouts must be positive and smoke timeout at most 3600 seconds"
        )
    try:
        return run(args.artifacts, args.timeout_seconds, args.smoke_timeout_seconds)
    except (OSError, RunnerError) as error:
        sys.stderr.write(str(error) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
