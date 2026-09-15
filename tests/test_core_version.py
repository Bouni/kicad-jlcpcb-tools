"""Tests for the core version helpers."""

import pytest

from core.version import is_supported_version, is_version7


@pytest.mark.parametrize(
    "version, is_seven, supported",
    [
        ("6.0.11", False, False),
        ("6.99.0", True, False),
        ("6.0.11-100-gabcdef-dirty", False, False),
        ("7.0", True, True),
        ("7.0.1", True, True),
        ("7.0.0-rc1", True, True),
        ("7.0.0-rc1-378-ge76fd128c3", True, True),
        ("7.0.2-2.fc42", True, True),
        ("7.0.1-rc1-378-ge76fd128c3", True, True),
        ("7.0.0-rc1-378-ge76fd128c3-dirty", True, True),
        ("7.0.1-distribution-extra", True, True),
        ("8.2.3", False, True),
        ("9.0.1-rc1", False, True),
        ("10.0.6", False, True),
        ("10.99.0-1234-gabcdef", False, True),
        ("10.99.0-1234-gabcdef-dirty", False, True),
    ],
)
def test_version(version: str, is_seven: bool, supported: bool) -> None:
    """Keep format dispatch distinct from the minimum supported host version."""
    assert is_version7(version) == is_seven
    assert is_supported_version(version) == supported


@pytest.mark.parametrize("version", ["", "unknown", "not a version"])
def test_unknown_version_is_unsupported(version: str) -> None:
    """An unrecognized host cannot bypass the launch guard."""
    assert not is_supported_version(version)
