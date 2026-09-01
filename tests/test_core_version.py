"""Tests for the core version helpers."""

from core.version import is_version6, is_version7


def test_version():
    """Tests for the various is_versionX() functions."""
    v6 = "6.1"
    v7 = "7.0.1"
    v72 = "7.0.2-2.fc42"
    v7rc1 = "7.0.1-rc1-378-ge76fd128c3"
    v8 = "8.2.3"
    v9 = "9.0.1-rc1"

    assert is_version6(v6)
    assert not is_version6(v7)

    assert is_version7(v7)
    assert is_version7(v72)
    assert is_version7(v7rc1)
    assert not is_version7(v8)

    assert not is_version6(v8)
    assert not is_version6(v9)
    assert not is_version7(v8)
    assert not is_version7(v9)
