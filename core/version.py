"""Contains helper function used all over the plugin."""

from packaging.version import Version


def _parse_version(version: str) -> Version:
    """Parse KiCad's version, including distribution and development suffixes."""
    # Select the API using the release before KiCad's dash-separated build suffixes.
    return Version(version.split("-", 1)[0])


def is_version7(version: str) -> bool:
    """Check if version is 7."""
    return Version("6.99") <= _parse_version(version) < Version("8.0")
