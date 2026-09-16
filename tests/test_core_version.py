"""Tests for the core version helpers."""

import pytest

from core.version import is_supported_version


@pytest.mark.parametrize(
    "version, supported",
    [
        ("6.0.11", False),
        ("6.99.0", False),
        ("6.0.11-100-gabcdef-dirty", False),
        ("7.0", True),
        ("7.0.1", True),
        ("7.0.0-rc1", True),
        ("7.0.0-rc1-378-ge76fd128c3", True),
        ("7.0.2-2.fc42", True),
        ("7.0.1-rc1-378-ge76fd128c3", True),
        ("7.0.0-rc1-378-ge76fd128c3-dirty", True),
        ("7.0.1-distribution-extra", True),
        ("8.2.3", True),
        ("9.0.1-rc1", True),
        ("10.0.6", True),
        ("10.99.0-1234-gabcdef", True),
        ("10.99.0-1234-gabcdef-dirty", True),
    ],
)
def test_version(version: str, supported: bool) -> None:
    """Enforce the minimum supported host version across KiCad build formats."""
    assert is_supported_version(version) == supported


@pytest.mark.parametrize("version", ["", "unknown", "not a version"])
def test_unknown_version_is_unsupported(version: str) -> None:
    """An unrecognized host cannot bypass the launch guard."""
    assert not is_supported_version(version)
