"""Contains helper function used all over the plugin."""

from packaging.version import InvalidVersion, Version


def _parse_version(version: str) -> Version:
    """Parse KiCad's version, including distribution and development suffixes."""
    # Select the API using the release before KiCad's dash-separated build suffixes.
    return Version(version.split("-", 1)[0])


def is_supported_version(version: str) -> bool:
    """Require KiCad 7.0 or newer before loading project-facing code."""
    try:
        return _parse_version(version) >= Version("7.0")
    except InvalidVersion:
        return False
