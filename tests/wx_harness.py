"""Scaffolding for tests that import plugin modules outside KiCad.

The plugin's GUI modules import wxPython and pcbnew at module load time and
reach their siblings through relative imports, so a test can only import one by
standing up a fake ``wx``, a fake ``pcbnew`` and a synthetic parent package
first.  Every test that needs one of those modules needs the same scaffolding,
so it lives here once instead of being rebuilt per test file.

Stubs installed through :func:`temporary_modules` are removed again afterwards,
which keeps one test file's fakes from becoming another's surprise.
"""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
import importlib
import importlib.util
from itertools import count
import logging
from pathlib import Path
import sys
import types
from typing import Any, Optional
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent

_MISSING = object()
_CORRECTION_PACKAGES = count()


def module(name, **symbols):
    """Create a module containing the explicitly supplied symbols."""
    created = types.ModuleType(name)
    created.__dict__.update(symbols)
    return created


@contextmanager
def temporary_modules(
    replacements: Mapping[str, types.ModuleType], *, namespaces: Iterable[str] = ()
) -> Iterator[None]:
    """Restore scoped module entries and supplied packages' child bindings."""
    roots = tuple(namespaces)

    def in_namespace(name: str) -> bool:
        return any(name == root or name.startswith(root + ".") for root in roots)

    def child_bindings(name: str, symbols: Mapping[str, Any]) -> set[str]:
        return {
            key
            for key, value in symbols.items()
            if isinstance(value, types.ModuleType) and value.__name__ == f"{name}.{key}"
        }

    names = set(replacements) | {name for name in sys.modules if in_namespace(name)}
    previous = {name: sys.modules.get(name, _MISSING) for name in names}
    packages = {
        name: (value, dict(vars(value)))
        for name, value in replacements.items()
        if in_namespace(name) and "__path__" in vars(value)
    }
    for name, (parent, symbols) in packages.items():
        for key in child_bindings(name, symbols):
            vars(parent).pop(key, None)
    for name in names:
        if in_namespace(name):
            sys.modules.pop(name, None)
    sys.modules.update(replacements)
    for name, value in replacements.items():
        parent_name, _, attribute = name.rpartition(".")
        if parent_name in packages:
            setattr(packages[parent_name][0], attribute, value)
    try:
        yield
    finally:
        for name, (parent, symbols) in packages.items():
            imported_children = {
                child[len(name) + 1 :]
                for child in sys.modules
                if child.startswith(name + ".") and "." not in child[len(name) + 1 :]
            }
            keys = (
                imported_children
                | child_bindings(name, vars(parent))
                | child_bindings(name, symbols)
            )
            for key in keys:
                if key in symbols:
                    vars(parent)[key] = symbols[key]
                else:
                    vars(parent).pop(key, None)
        for name in tuple(sys.modules):
            if in_namespace(name):
                sys.modules.pop(name, None)
        for name, restored in previous.items():
            if restored is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = restored


class FakeWxModule(types.ModuleType):
    """A wx-shaped module that mints the flag constants it is asked for.

    ``UPPER_CASE`` names become distinct single-bit values, so a test can
    assert on style arithmetic (``style & wx.ICON_WARNING``) without two
    unrelated flags colliding, and no test has to hand-number its constants.

    Everything else -- classes, functions, submodules -- must be supplied
    explicitly by the caller.  An attribute nobody supplied raises
    ``AttributeError`` rather than resolving to something callable, so a
    misspelled wx call fails the test instead of quietly succeeding.
    """

    def __init__(self, name, **symbols):
        super().__init__(name)
        self.__dict__["_bits"] = count()
        self.__dict__.update(symbols)

    def __getattr__(self, name):
        """Mint a flag; reject any other attribute that was not supplied."""
        if not name.isupper() or name.startswith("__"):
            raise AttributeError(name)
        value = 1 << next(self._bits)
        setattr(self, name, value)
        return value


def wx_stubs(
    *, submodules: Iterable[str] = ("dataview", "adv"), **symbols: Any
) -> dict[str, types.ModuleType]:
    """Return ``{name: module}`` for a fake ``wx`` and the requested submodules."""
    wx = FakeWxModule("wx", **{"NOT_FOUND": -1, **symbols})
    wx.__path__ = []
    stubs = {"wx": wx}
    for submodule in submodules:
        child = FakeWxModule(f"wx.{submodule}")
        setattr(wx, submodule, child)
        stubs[f"wx.{submodule}"] = child
    return stubs


def package_stubs(package, submodules=()):
    """Return a synthetic parent package, plus any subpackages needed under it."""
    root = module(package)
    root.__path__ = [str(ROOT)]
    stubs = {package: root}
    for submodule in submodules:
        child = module(f"{package}.{submodule}")
        child.__path__ = []
        stubs[f"{package}.{submodule}"] = child
    return stubs


def load(
    package: str, name: str, replacements: Mapping[str, types.ModuleType]
) -> types.ModuleType:
    """Load ``<name>.py`` from the repo root as ``package.name`` under the stubs."""
    module_name = f"{package}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    loaded.__package__ = package
    namespaces = ("wx",) if "wx" in replacements else ()
    with temporary_modules(
        {**replacements, module_name: loaded}, namespaces=namespaces
    ):
        spec.loader.exec_module(loaded)
    return loaded


@contextmanager
def load_siblings(
    package: str,
    names: Iterable[str],
    replacements: Mapping[str, types.ModuleType],
) -> Iterator[dict[str, types.ModuleType]]:
    """Keep real siblings and their dependencies registered for the whole scope.

    Explicit replacements take precedence over real imports. A fresh synthetic
    parent is provided unless the caller supplies one, so real package startup
    never runs. All prior entries under the package are restored even if an
    import or the calling test raises; transitive and deferred siblings created
    inside the scope are removed.
    """
    stubs = {**package_stubs(package), **replacements}
    namespaces = (package, "wx") if "wx" in replacements else (package,)
    with temporary_modules(stubs, namespaces=namespaces):
        loaded = {name: importlib.import_module(f"{package}.{name}") for name in names}
        yield loaded


@contextmanager
def load_correction_modules(
    *,
    package: Optional[str] = None,  # noqa: UP045
    wx: Optional[Mapping[str, types.ModuleType]] = None,  # noqa: UP045
    pcbnew: Optional[types.ModuleType] = None,  # noqa: UP045
    names: Iterable[str] = (),
    replacements: Optional[Mapping[str, types.ModuleType]] = None,  # noqa: UP045
) -> Iterator[types.SimpleNamespace]:
    """Compose real correction siblings with explicit, scoped GUI substitutes.

    Additional siblings share the same package, wx object, events, and correction
    classes. Callers can supply their own wx and pcbnew controls and replace any
    other dependencies; none are removed until the calling fixture exits.
    """
    package_name = package or f"_correction_tests_{next(_CORRECTION_PACKAGES)}"
    stubs = (
        dict(wx)
        if wx is not None
        else wx_stubs(
            Dialog=type("Dialog", (), {}),
            PostEvent=MagicMock(),
            MessageBox=MagicMock(),
            MessageDialog=MagicMock(),
            NOT_FOUND=-1,
        )
    )
    if pcbnew is not None:
        stubs["pcbnew"] = pcbnew
    stubs.update(replacements or {})
    siblings = tuple(
        dict.fromkeys(("library", "corrections", "correction_data", *names))
    )
    with load_siblings(package_name, siblings, stubs) as loaded:
        yield types.SimpleNamespace(
            **loaded,
            data=loaded["correction_data"],
            wx=stubs["wx"],
            package_name=package_name,
        )


def mainwindow_stubs(
    package: str,
    *,
    wx: Optional[Mapping[str, types.ModuleType]] = None,  # noqa: UP045
    pcbnew: Optional[types.ModuleType] = None,  # noqa: UP045
    **overrides: Mapping[str, Any],
) -> dict[str, types.ModuleType]:
    """Return every module ``mainwindow.py`` imports, stubbed.

    Defaults behave like the real thing where a caller is likely to depend on a
    return value, and are inert everywhere else.  Pass ``overrides`` keyed by
    module suffix to replace one module's symbols, e.g.
    ``mainwindow_stubs(pkg, helpers={"getVersion": lambda: "1.2.3"})``.
    """
    stubs = package_stubs(package, ("bom_estimation", "enrichment"))
    stubs.update(wx_stubs() if wx is None else wx)
    stubs["pcbnew"] = module("pcbnew") if pcbnew is None else pcbnew

    # events.py falls back to a plain event factory when wx.lib is missing.
    # The stub submodules are limited to the ones wired up above and the fake
    # wx has an empty __path__, so wx.lib cannot be found and the fallback is
    # taken here -- even where wxPython is installed.  Loading the real module
    # keeps its event names from drifting out of step with a copy.
    stubs[f"{package}.events"] = load(package, "events", stubs)

    symbols = {
        "bom_estimation.assembly_mode": {
            "classify_component_product_type": lambda _value: None
        },
        "bom_estimation.help_text": {
            "show_bom_estimator_help": lambda *_args, **_kwargs: None
        },
        "bom_widget": {"BomEstimatorController": object, "BomEstimatorWidget": object},
        "corrections": {"CorrectionManagerDialog": object},
        "datamodel": {
            "PartListDataModel": type("PartListDataModel", (), {"columns": {}}),
            "STANDARD_ONLY_TOOLTIP": "",
        },
        "dataview_highlight": {
            "HighlightedTextRenderer": object,
            "decode_highlighted_value": lambda value: (value, []),
            "simplify_footprint_name": lambda value: value,
        },
        "derive_params": {"params_for_part": lambda _details: "params"},
        "enrichment.providers": {"LCSCAssemblyMetadataProvider": object},
        "fabrication": {"Fabrication": object},
        "footprint_helpers": {
            "get_is_dnp": lambda _footprint: False,
            "set_lcsc_value": lambda *_args: None,
            "toggle_exclude_from_bom": lambda _footprint: None,
            "toggle_exclude_from_pos": lambda _footprint: None,
        },
        "generate_hooks": {
            "format_hook_error": str,
            "run_configured_hook": lambda **_kwargs: None,
        },
        "helpers": {
            "PLUGIN_PATH": str(ROOT),
            "GetScaleFactor": lambda _window: 1,
            "HighResWxSize": lambda _window, size: size,
            "getVersion": lambda: "test",
            "loadBitmapScaled": lambda *_args: None,
        },
        "kicad_drc": {"DRCViolationCounter": object},
        "library": {
            "Library": object,
            "LibraryState": types.SimpleNamespace(
                INITIALIZED=object(), UPDATE_NEEDED=object()
            ),
            "CorrectionState": Enum(
                "CorrectionState", ("READY", "NEEDS_REPAIR", "UNAVAILABLE")
            ),
        },
        "partdetails": {"PartDetailsDialog": object},
        "partmapper": {"PartMapperManagerDialog": object},
        "partselector": {"PartSelectorDialog": object},
        "schematicexport": {"SchematicExport": object},
        "settings": {"SettingsDialog": object},
        "store": {"Store": object},
        "why_standard_dialog": {"WhyStandardDialog": object},
    }
    symbols.update(overrides)
    stubs.update(
        {
            f"{package}.{suffix}": module(f"{package}.{suffix}", **values)
            for suffix, values in symbols.items()
        }
    )
    return stubs


def load_mainwindow(package, *, wx=None, pcbnew=None, **overrides):
    """Load ``mainwindow.py`` under ``package`` with every dependency stubbed.

    The loaded module keeps its own reference to the fake wx, so a caller that
    needs to configure or assert on it can reach it as ``module.wx``.
    """
    stubs = mainwindow_stubs(package, wx=wx, pcbnew=pcbnew, **overrides)

    # mainwindow.py raises the requests/urllib3 log levels as an import side
    # effect; leaving that in place would leak into unrelated tests.
    levels = {name: logging.getLogger(name).level for name in ("requests", "urllib3")}
    try:
        return load(package, "mainwindow", stubs)
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
