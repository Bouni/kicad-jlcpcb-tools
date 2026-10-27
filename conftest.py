"""Keep optional desktop checks strict when a CI lane promises native coverage."""

from collections.abc import Generator
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from typing import Any, Optional

import pytest

_NATIVE_RUNTIME = pytest.StashKey[str]()
_CAPABILITY_SKIP = pytest.StashKey[bool]()
# KiCad 10 added design variants; a KiCad 9 board has none.
_VARIANT_API = ("GetVariantNamesForUI", "AddVariant")
# The KiCad majors whose golden fabrication outputs are recorded.
_GOLDEN_BUILDS = Path(__file__).parent / "tests" / "golden" / "kicad_builds.json"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Allow native CI checks to require their runtime instead of skipping."""
    parser.addoption(
        "--require-native",
        choices=("wx", "kicad"),
        default=os.environ.get("KICAD_JLCPCB_REQUIRE_NATIVE"),
        help="Require the native runtime, selected tests, and non-skipped results.",
    )
    parser.addoption(
        "--kicad-major",
        choices=("7", "8", "9", "10"),
        default=os.environ.get("KICAD_JLCPCB_KICAD_MAJOR", "10"),
        help="The KiCad major version a required native KiCad lane must find.",
    )


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: list[Any]) -> None:
    """Own private wx desktops and state before workers parse configuration."""
    if config.getoption("require_native") != "wx":
        return
    from pyvirtualdisplay import Display

    resources = ExitStack()
    config.add_cleanup(resources.close)
    for spec in specs:
        root = Path(
            resources.enter_context(tempfile.TemporaryDirectory(prefix="native-wx-"))
        )
        environment = {}
        for key, name in (
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_RUNTIME_DIR", "runtime"),
            ("KICAD_CONFIG_HOME", "kicad"),
            ("TMPDIR", "tmp"),
        ):
            directory = root / name
            directory.mkdir(mode=0o700)
            environment[key] = str(directory)
        # wx closes its display probe before GTK opens the real connection.
        display = Display(
            size=(1920, 1080), manage_global_env=False, extra_args=["-noreset"]
        )
        resources.callback(display.stop)
        display.start()
        environment["DISPLAY"] = display.new_display_var
        # Only cache options enter the environment. Exact-node child pytest
        # processes inherit this desktop without inheriting CLI worker flags.
        options = shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
        environment["PYTEST_ADDOPTS"] = shlex.join(
            options + ["-o", f"cache_dir={environment['XDG_CACHE_HOME']}"]
        )
        spec.env.update(environment)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Check installed modules before collection can supply GUI/board doubles."""
    mode = session.config.getoption("require_native")
    if not mode:
        return
    # Native input tests spawn pytest children; they must inherit strictness.
    os.environ["KICAD_JLCPCB_REQUIRE_NATIVE"] = mode
    os.environ["KICAD_JLCPCB_NATIVE_TESTS"] = "1"
    try:
        wx = importlib.import_module("wx")
        grid = importlib.import_module("wx.grid")
        if not callable(grid.Grid):
            raise TypeError("wx.grid.Grid is unavailable")
        runtime = f"wx: {wx.version()}"
        if mode == "kicad":
            pcbnew = importlib.import_module("pcbnew")
            version = str(pcbnew.GetBuildVersion())
            expected = session.config.getoption("kicad_major")
            if _kicad_major(version) != expected:
                raise TypeError(f"expected KiCad {expected}, found {version!r}")
            # Child processes of isolated native tests check the same KiCad.
            os.environ["KICAD_JLCPCB_KICAD_MAJOR"] = expected
            if int(expected) >= 10:
                for method in _VARIANT_API:
                    if not callable(getattr(pcbnew.BOARD, method, None)):
                        raise TypeError(f"KiCad 10 BOARD.{method} is unavailable")
            runtime += f"; KiCad: {version}"
    except (ImportError, OSError, AttributeError, TypeError, RuntimeError) as error:
        raise pytest.UsageError(
            f"Required native {mode} runtime unavailable: {error}"
        ) from error
    session.config.stash[_NATIVE_RUNTIME] = runtime


def pytest_report_header(config: pytest.Config) -> Optional[str]:
    """Include the actual binding versions in native CI logs."""
    return config.stash.get(_NATIVE_RUNTIME, None)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reduce per-process contention by running strict Python budgets after bulk work."""
    items.sort(
        key=lambda item: item.path.name
        in {
            "test_impedance_spacing_performance.py",
            "test_impedance_performance_budget.py",
        }
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Reject a green run in which none of the promised native tests would run."""
    _skip_variant_tests_without_the_api(session.items)
    _skip_standalone_drc_where_it_crashes(session.items)
    _skip_golden_tests_without_recorded_outputs(session.items)
    mode = session.config.getoption("require_native")
    if mode and not any(
        item.get_closest_marker(f"native_{mode}")
        and not item.stash.get(_CAPABILITY_SKIP, False)
        for item in session.items
    ):
        raise pytest.UsageError(f"No native_{mode} tests selected that would run")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, Any, None]:
    """Reject partial collection before skipped modules can hide required tests."""
    report = (yield).get_result()
    if collector.config.getoption("require_native") and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"Required native collection skipped: {report.longrepr}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, Any, None]:
    """Retain worker identity and fail unexpected native fixture skips."""
    if call.when == "setup":
        worker = getattr(item.config, "workerinput", {}).get("workerid")
        if worker:
            item.user_properties.append(("native_worker_id", worker))
    report = (yield).get_result()
    mode = item.config.getoption("require_native")
    if (
        mode
        and report.skipped
        and item.get_closest_marker(f"native_{mode}")
        and not item.stash.get(_CAPABILITY_SKIP, False)
    ):
        report.outcome = "failed"
        report.longrepr = f"Required native test skipped: {report.longrepr}"


def _skip_variant_tests_without_the_api(items: list[pytest.Item]) -> None:
    """Skip tests of KiCad design variants where the running KiCad has none.

    The skip follows the API a test needs, not a version number. A required
    native KiCad lane accepts it and the two skips below, no other.
    """
    marked = [item for item in items if item.get_closest_marker("kicad_variants")]
    if not marked:
        return
    try:
        pcbnew = importlib.import_module("pcbnew")
    except ImportError:
        return  # The tests' own fixtures report a missing KiCad.
    missing = [
        name for name in _VARIANT_API if not callable(getattr(pcbnew.BOARD, name, None))
    ]
    if missing:
        reason = f"KiCad has no design variants (BOARD.{missing[0]} is unavailable)"
        for item in marked:
            item.stash[_CAPABILITY_SKIP] = True
            item.add_marker(pytest.mark.skip(reason=reason))


def _skip_standalone_drc_where_it_crashes(items: list[pytest.Item]) -> None:
    """Skip tests that run KiCad's DRC outside KiCad on KiCad 10, where it crashes.

    KiCad 10's WriteDRCReport runs the library-parity check, which asks
    Pgm() for the library manager; outside the KiCad process Pgm() is null,
    and the call segfaults. The function exists and crashes, so no API check
    can tell, and this is the one skip keyed to the running version number.
    The plugin itself runs inside KiCad, where Pgm() exists.
    """
    marked = [item for item in items if item.get_closest_marker("kicad_standalone_drc")]
    if not marked:
        return
    try:
        pcbnew = importlib.import_module("pcbnew")
    except ImportError:
        return  # The tests' own fixtures report a missing KiCad.
    if _kicad_major(str(pcbnew.GetBuildVersion())) != "10":
        return
    reason = (
        "KiCad 10's standalone WriteDRCReport segfaults: its library-parity check "
        "calls PROJECT_PCB::FootprintLibAdapter, which dereferences a null Pgm() "
        "outside the KiCad process (KiCad issue #25755: "
        "https://gitlab.com/kicad/code/kicad/-/issues/25755)"
    )
    for item in marked:
        item.stash[_CAPABILITY_SKIP] = True
        item.add_marker(pytest.mark.skip(reason=reason))


def _skip_golden_tests_without_recorded_outputs(items: list[pytest.Item]) -> None:
    """Skip golden-output tests on a KiCad whose outputs are not recorded.

    tests/golden/kicad_builds.json lists the KiCad majors with recorded
    outputs. The skip follows the record, so recording a major's outputs
    brings its golden tests back. Update mode (KICAD_JLCPCB_GOLDEN_UPDATE)
    runs them on any major, since that is how a major's outputs are first
    recorded.
    """
    marked = [item for item in items if item.get_closest_marker("kicad_golden")]
    if not marked:
        return
    try:
        pcbnew = importlib.import_module("pcbnew")
    except ImportError:
        return  # The tests' own fixtures report a missing KiCad.
    major = _kicad_major(str(pcbnew.GetBuildVersion()))
    try:
        recorded = json.loads(_GOLDEN_BUILDS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        recorded = {}
    # As tests/test_golden_outputs.py reads it: unset, empty or "0" compares.
    update = os.environ.get("KICAD_JLCPCB_GOLDEN_UPDATE", "").strip()
    if major is None or major in recorded or update not in ("", "0"):
        return
    reason = (
        f"No golden outputs are recorded for KiCad {major}: "
        "tests/golden/kicad_builds.json lists the KiCad majors whose outputs "
        "are recorded"
    )
    for item in marked:
        item.stash[_CAPABILITY_SKIP] = True
        item.add_marker(pytest.mark.skip(reason=reason))


def _kicad_major(version: str) -> Optional[str]:
    """Return the major version in a KiCad build string, or None."""
    found = re.match(r"\s*(\d+)\.", version)
    return found.group(1) if found else None
