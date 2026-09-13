"""Keep optional desktop checks strict when a CI lane promises native coverage."""

from collections.abc import Generator
import importlib
import os
from typing import Any, Optional

import pytest

_NATIVE_RUNTIME = pytest.StashKey[str]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Allow native CI checks to require their runtime instead of skipping."""
    parser.addoption(
        "--require-native",
        choices=("wx", "kicad"),
        default=os.environ.get("KICAD_JLCPCB_REQUIRE_NATIVE"),
        help="Require the native runtime, selected tests, and non-skipped results.",
    )


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
            for method in ("GetVariantNamesForUI", "AddVariant"):
                if not callable(getattr(pcbnew.BOARD, method, None)):
                    raise TypeError(f"KiCad 10 BOARD.{method} is unavailable")
            runtime += f"; KiCad: {pcbnew.GetBuildVersion()}"
    except (ImportError, OSError, AttributeError, TypeError, RuntimeError) as error:
        raise pytest.UsageError(
            f"Required native {mode} runtime unavailable: {error}"
        ) from error
    session.config.stash[_NATIVE_RUNTIME] = runtime


def pytest_report_header(config: pytest.Config) -> Optional[str]:
    """Include the actual binding versions in native CI logs."""
    return config.stash.get(_NATIVE_RUNTIME, None)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Reject a green run that selected none of the promised native tests."""
    mode = session.config.getoption("require_native")
    if mode and not any(
        item.get_closest_marker(f"native_{mode}") for item in session.items
    ):
        raise pytest.UsageError(f"No native_{mode} tests selected")


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
    """Fail late fixture skips as well as missing-runtime preflight failures."""
    report = (yield).get_result()
    mode = item.config.getoption("require_native")
    if mode and report.skipped and item.get_closest_marker(f"native_{mode}"):
        report.outcome = "failed"
        report.longrepr = f"Required native test skipped: {report.longrepr}"
