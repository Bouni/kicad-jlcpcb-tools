"""Tests for the core version helpers."""

import pytest

from core.version import is_version7


@pytest.mark.parametrize(
    "version, is_seven",
    [
        ("6.0.11", False),
        ("6.99.0", True),
        ("6.0.11-100-gabcdef-dirty", False),
        ("7.0", True),
        ("7.0.1", True),
        ("7.0.0-rc1", True),
        ("7.0.0-rc1-378-ge76fd128c3", True),
        ("7.0.2-2.fc42", True),
        ("7.0.1-rc1-378-ge76fd128c3", True),
        ("7.0.0-rc1-378-ge76fd128c3-dirty", True),
        ("7.0.1-distribution-extra", True),
        ("8.2.3", False),
        ("9.0.1-rc1", False),
        ("10.0.6", False),
        ("10.99.0-1234-gabcdef", False),
        ("10.99.0-1234-gabcdef-dirty", False),
    ],
)
def test_version(version: str, is_seven: bool) -> None:
    """Pick KiCad 7's schematic format by the host's version."""
    assert is_version7(version) == is_seven
