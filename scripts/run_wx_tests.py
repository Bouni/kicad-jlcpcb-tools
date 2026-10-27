"""Finish parallel native controls before running the complete Python budgets."""

import argparse
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any
from xml.etree import ElementTree as ET

from scripts.native_impedance_support import ProcessSupervisor, write_json

BUDGETS = (
    "tests/test_impedance_spacing_performance.py",
    "tests/test_impedance_performance_budget.py",
)
SELECTION = "not native_kicad and not os_input"


def merge_results(paths: list[Path], output: Path) -> bool:
    """Preserve raw suites and outcomes, rejecting incomplete or duplicate reports."""
    combined = ET.Element("testsuites")
    identities: set[tuple[str, str]] = set()
    failed = False
    for path in paths:
        root = ET.parse(path).getroot()  # noqa: S314 - owned pytest reports
        suites = [root] if root.tag == "testsuite" else list(root)
        if root.tag not in {"testsuite", "testsuites"} or not suites:
            raise ValueError(f"Invalid pytest report: {path}")
        phase_identities = set()
        for suite in suites:
            cases = list(suite.iter("testcase"))
            if suite.tag != "testsuite" or int(suite.get("tests", "-1")) != len(cases):
                raise ValueError(f"Incomplete pytest report: {path}")
            for attribute, outcome in (
                ("failures", "failure"),
                ("errors", "error"),
                ("skipped", "skipped"),
            ):
                if int(suite.get(attribute, "0")) != sum(
                    len(case.findall(outcome)) for case in cases
                ):
                    raise ValueError(f"Inconsistent pytest {attribute}: {path}")
            for case in cases:
                classname, name = case.get("classname"), case.get("name")
                if classname and name:
                    phase_identities.add((classname, name))
                failed |= any(
                    case.find(outcome) is not None for outcome in ("failure", "error")
                )
            combined.append(suite)
        if identities & phase_identities:
            raise ValueError(f"Overlapping pytest phases: {path}")
        identities.update(phase_identities)
    temporary = output.with_suffix(".xml.tmp")
    ET.ElementTree(combined).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(output)
    return failed


def run(artifacts: Path) -> int:
    """Run both phases after ordinary failures; retain partial diagnostics on signals."""
    phases = (
        (
            "bulk",
            [
                "-n",
                "auto",
                "--maxprocesses=4",
                "--dist=loadfile",
                "--no-loadscope-reorder",
                "--max-worker-restart=0",
                "--require-native=wx",
                "-m",
                SELECTION,
                *("--ignore=" + path for path in BUDGETS),
            ],
        ),
        ("budgets", ["-n", "0", *BUDGETS]),
    )
    summary: dict[str, Any] = {"status": "FAIL", "pid": os.getpid(), "phases": []}
    interrupted = 0

    def interrupt(number: int, _frame: object) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = number
            raise KeyboardInterrupt

    previous = {
        number: signal.signal(number, interrupt)
        for number in (signal.SIGTERM, signal.SIGINT)
    }
    status = 0
    try:
        with ProcessSupervisor() as supervisor:
            for name, options in phases:
                phase: dict[str, Any] = {"name": name, "started_at": time.time()}
                summary["phases"].append(phase)
                write_json(artifacts / "phases.json", summary)
                environment = dict(os.environ)
                if name == "budgets":
                    environment.pop("KICAD_JLCPCB_REQUIRE_NATIVE", None)
                sys.stdout.write(f"Starting wx {name} phase\n")
                sys.stdout.flush()
                phase.update(
                    supervisor.run(
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            "-rs",
                            f"--junitxml={artifacts / ('pytest-' + name + '.xml')}",
                            *options,
                        ],
                        Path.cwd(),
                        environment,
                        artifacts / f"pytest-{name}.log",
                        900,
                    )
                )
                phase["finished_at"] = time.time()
                status |= int(phase["returncode"] != 0 or phase["timed_out"])
                sys.stdout.write((artifacts / f"pytest-{name}.log").read_text())
                sys.stdout.flush()
        failed = merge_results(
            [artifacts / f"pytest-{name}.xml" for name, _ in phases],
            artifacts / "pytest.xml",
        )
        status = status or int(failed)
        summary["status"] = "FAIL" if status else "PASS"
    except KeyboardInterrupt:
        status = 128 + interrupted if interrupted else 130
        summary["status"] = "CANCELLED"
    except (OSError, ValueError, ET.ParseError) as error:
        status = 1
        summary["error"] = str(error)
    finally:
        write_json(artifacts / "phases.json", summary)
        for number, handler in previous.items():
            signal.signal(number, handler)
    return status


def main() -> int:
    """Accept the lane's existing artifact directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    return run(parser.parse_args().artifacts)


if __name__ == "__main__":
    raise SystemExit(main())
