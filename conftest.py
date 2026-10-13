"""Keep optional desktop checks strict when a CI lane promises native coverage."""

from collections.abc import Generator
import importlib
import os
import re
from typing import Any, Optional

import pytest

_NATIVE_RUNTIME = pytest.StashKey[str]()
_CAPABILITY_SKIP = pytest.StashKey[bool]()
# KiCad 10 added design variants; a KiCad 9 board has none.
_VARIANT_API = ("GetVariantNamesForUI", "AddVariant")


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
        choices=("9", "10"),
        default=os.environ.get("KICAD_JLCPCB_KICAD_MAJOR", "10"),
        help="The KiCad major version a required native KiCad lane must find.",
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
            version = str(pcbnew.GetBuildVersion())
            expected = session.config.getoption("kicad_major")
            found = re.match(r"\s*(\d+)\.", version)
            if found is None or found.group(1) != expected:
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


def pytest_collection_finish(session: pytest.Session) -> None:
    """Reject a green run in which none of the promised native tests would run."""
    _skip_variant_tests_without_the_api(session.items)
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
    """Fail late fixture skips as well as missing-runtime preflight failures."""
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

    The skip follows the API a test needs, not a version number, and it is
    the only skip a required native KiCad lane accepts.
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
