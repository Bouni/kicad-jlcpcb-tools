"""Check setuptools impedance declarations and the original workbook resource."""

import ast
import hashlib
from pathlib import Path
import re

from impedance import workbook

_ROOT = Path(__file__).resolve().parent.parent


def _toml_array(table: str, key: str) -> list[str]:
    """Read one simple packaging string array without a Python 3.11 dependency."""
    source = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(rf"(?ms)^\[{re.escape(table)}\]\s*\n(.*?)(?=^\[|\Z)", source)
    assert section is not None, f"Missing packaging table: {table}"
    assignment = re.search(
        rf"(?ms)^{re.escape(key)}\s*=\s*(\[[^\]]*\])", section.group(1)
    )
    assert assignment is not None, f"Missing packaging setting: {table}.{key}"
    values = ast.literal_eval(assignment.group(1))
    assert isinstance(values, list) and all(isinstance(value, str) for value in values)
    return values


def test_setuptools_includes_impedance_package_and_vendor_resource() -> None:
    """Installed package data must resolve the writer's exact workbook resource."""
    assert "impedance" in _toml_array("tool.setuptools", "packages")
    patterns = _toml_array("tool.setuptools.package-data", "impedance")
    package_root = _ROOT / "impedance"
    declared_resources = {
        path.resolve() for pattern in patterns for path in package_root.glob(pattern)
    }
    assert workbook.TEMPLATE_PATH.resolve() in declared_resources
    assert workbook.TEMPLATE_PATH.relative_to(package_root) == Path(
        "resources/Required_impedance_control.xlsx"
    )
    assert (
        hashlib.sha256(workbook.TEMPLATE_PATH.read_bytes()).hexdigest()
        == workbook.TEMPLATE_SHA256
    )
