"""Keep KiCad 10 fixture integrations separate from older native compatibility lanes."""

from collections import Counter
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CASES = {
    "tests/test_impedance_capture_examples.py": 20,
    "tests/test_impedance_connector_captures.py": 2,
    "tests/test_impedance_metadata_native.py": 3,
    "tests/test_impedance_netclass_fixtures.py": 28,
    "tests/test_rf_impedance_fixture_realism.py": 16,
    "tests/test_rf_impedance_native.py": 168,
    "tests/test_rf_impedance_showcases.py": 2,
}


def _matrix_selections() -> dict[str, str]:
    """Resolve the marker argument actually passed by each native workflow lane."""
    source = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    native_job = source.split("  native-kicad:\n", 1)[1]
    matrix = native_job.split("        include:\n", 1)[1].split("    env:\n", 1)[0]
    rows = re.findall(
        r'          - kicad: "(\d+)"\n(.*?)(?=          - kicad:|\Z)', matrix, re.S
    )
    command = re.search(r"--kicad-major=.*? -m (.*?) \\\n", native_job)
    assert command is not None, (
        "Native lane must pass an explicit pytest marker selection"
    )
    template = command[1].strip("'\"")
    result = {}
    for major, settings in rows:
        selector = re.search(r"^            selection: (.+)$", settings, re.M)
        expression = template.replace(
            "${{ matrix.selection }}", selector[1] if selector else ""
        )
        assert expression, f"KiCad {major} has no resolved test selection"
        result[major] = expression
    assert set(result) == {"7", "8", "9", "10"}
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
        if "native_kicad" in item["markers"]
        and item["nodeid"].split("::")[0] in FIXTURE_CASES
    }
    assert Counter(node.split("::")[0] for node in expected) == FIXTURE_CASES
    marked = {
        item["nodeid"]
        for item in collection["items"]
        if "kicad10_impedance" in item["markers"]
    }
    assert marked == expected


@pytest.mark.parametrize("major", ["7", "8", "9", "10"])
def test_native_matrix_selects_all_supported_checks_and_required_fixture_cases(
    native_collection: Callable[[str], dict[str, Any]], major: str
) -> None:
    """Evaluate actual CI selections; old lanes retain every other native test."""
    all_native = set(native_collection("native_kicad")["selected"])
    fixture_cases = {
        node for node in all_native if node.split("::")[0] in FIXTURE_CASES
    }
    supported = all_native - fixture_cases
    assert len(fixture_cases) == 239
    assert supported, "Existing native compatibility/fallback coverage must remain"
    selected = set(native_collection(_matrix_selections()[major])["selected"])
    assert selected == (all_native if major == "10" else supported)
