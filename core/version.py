"""Contains helper function used all over the plugin."""

import re

from packaging.version import Version


def _is_version_in_range(version: str, min_version: str, max_version: str) -> bool:
    """Check if version is in range. Must comply with https://packaging.python.org/en/latest/specifications/version-specifiers/#version-specifiers."""
    # Remove any trailing '.fc42', '+gXXXX', or similar after the first long segment
    ver = Version(re.sub(r"([^-]+(?:-[^-]+)*)-.*", r"\1", version))
    return Version(min_version) <= ver < Version(max_version)


def is_version7(version: str) -> bool:
    """Check if version is 7."""
    return _is_version_in_range(version, "6.99", "8.0")


def is_version6(version: str) -> bool:
    """Check if version is 6."""
    return _is_version_in_range(version, "5.99", "7.0")
