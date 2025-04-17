"""Contains helper function used all over the plugin."""

from packaging.version import Version


def _is_version_in_range(version: str, min_version: str, max_version: str) -> bool:
    """Check if version is in range. Must comply with https://packaging.python.org/en/latest/specifications/version-specifiers/#version-specifiers."""
    ver = Version(version)
    return Version(min_version) <= ver < Version(max_version)


def is_version7(version: str) -> bool:
    """Check if version is 7."""
    return _is_version_in_range(version, "6.99", "8.0")


def is_version6(version: str) -> bool:
    """Check if version is 6."""
    return _is_version_in_range(version, "5.99", "7.0")


def test_version():
    """Tests for the various is_versionX() functions."""
    v6 = "6.1"
    v7 = "7.0.1"
    v8 = "8.2.3"
    v9 = "9.0.1-rc1"

    assert is_version6(v6)
    assert not is_version6(v7)

    assert is_version7(v7)
    assert not is_version7(v8)

    assert not is_version6(v8)
    assert not is_version6(v9)
    assert not is_version7(v8)
    assert not is_version7(v9)
