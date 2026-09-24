"""Load bundled report dependencies without replacing another plugin's modules.

Only this private namespace participates in the finder. Vendor source files and
their relative imports are unchanged; absolute imports between the three bundled
packages are redirected by each vendor module's own import function. Public
imports, the process-wide builtins, and public ``sys.modules`` keys stay intact.
"""

import builtins
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder, SourceFileLoader
from importlib.util import spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Optional

_PACKAGES = frozenset({"websockets", "openpyxl", "et_xmlfile"})
_PREFIX = __name__ + "."
_LIB = Path(__file__).resolve().parents[2] / "lib"
_BUNDLED = _LIB.exists()
_STANDARD_IMPORT = builtins.__import__
_INSTALLED_ROOTS: dict[str, Path] = {}
_REQUIREMENTS = {
    "websockets": ">=15.0.1,<16",
    "openpyxl": "==3.1.5",
    "et_xmlfile": "==2.0.0",
}


def _package_root(name: str) -> Path:
    """Use installed sources only for setuptools layouts without bundled lib."""
    if _BUNDLED:
        return _LIB / name
    if name not in _INSTALLED_ROOTS:
        # PathFinder searches files independently of an older public package
        # another plugin already imported into sys.modules.
        spec = PathFinder.find_spec(name)
        if spec is None or spec.origin is None or not Path(spec.origin).is_file():
            raise ImportError(
                f"Missing {name} dependency. Install or update the complete plugin "
                "package or its declared standalone Python dependencies."
            )
        _INSTALLED_ROOTS[name] = Path(spec.origin).parent
    return _INSTALLED_ROOTS[name]


def _check_installed_version(module: ModuleType, name: str) -> None:
    """Preserve the versions required by pyproject for non-PCM installations."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    version = getattr(module, "__version__", "unknown")
    try:
        supported = Version(str(version)) in SpecifierSet(_REQUIREMENTS[name])
    except InvalidVersion:
        supported = False
    if not supported:
        raise ImportError(
            f"Unsupported {name} dependency version {version!r}. Install or update "
            "the complete plugin package or its declared standalone Python dependencies."
        )


def _vendor_import(
    name: str,
    globals: Optional[dict[str, Any]] = None,
    locals: Optional[dict[str, Any]] = None,
    fromlist: Optional[Sequence[str]] = (),
    level: int = 0,
) -> Any:
    """Resolve vendor absolute imports without binding their public names."""
    root = name.partition(".")[0]
    if level == 0 and root in _PACKAGES:
        imported = _STANDARD_IMPORT(_PREFIX + name, globals, locals, fromlist, 0)
        # __import__('a.b') returns a, whereas private a lives below our prefix.
        # Preserve the object that a vendor's `import a.b` binds to its name a.
        return imported if fromlist else sys.modules[_PREFIX + root]
    return _STANDARD_IMPORT(name, globals, locals, fromlist, level)


class _VendorLoader(SourceFileLoader):
    """Give only private vendor modules a package-aware import function."""

    def exec_module(self, module: ModuleType) -> None:
        """Retain normal import locks and failure cleanup from importlib."""
        module.__dict__["__builtins__"] = dict(
            vars(builtins), __import__=_vendor_import
        )
        super().exec_module(module)
        name = module.__name__[len(_PREFIX) :]
        if not _BUNDLED and name in _PACKAGES:
            _check_installed_version(module, name)


class _VendorFinder(MetaPathFinder):
    """Find only the three known packages below this plugin's private prefix."""

    _jlcpcb_vendor_prefix = _PREFIX

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]] = None,
        target: Optional[ModuleType] = None,
    ) -> Optional[ModuleSpec]:
        """Keep installed packages and other plugins outside the lookup path."""
        if not fullname.startswith(_PREFIX):
            return None
        parts = fullname[len(_PREFIX) :].split(".")
        if parts[0] not in _PACKAGES or not all(part.isidentifier() for part in parts):
            return None
        candidate = _package_root(parts[0]).joinpath(*parts[1:])
        source = (
            candidate / "__init__.py"
            if candidate.is_dir()
            else candidate.with_suffix(".py")
        )
        if not source.is_file():
            if len(parts) == 1:
                raise ImportError(
                    f"Missing bundled {parts[0]} dependency. Install or update "
                    "the complete plugin package."
                )
            return None
        return spec_from_file_location(
            fullname, source, loader=_VendorLoader(fullname, str(source))
        )


for _index, _finder in enumerate(sys.meta_path):
    if getattr(_finder, "_jlcpcb_vendor_prefix", None) == _PREFIX:
        sys.meta_path[_index] = _VendorFinder()
        break
else:
    sys.meta_path.insert(0, _VendorFinder())
