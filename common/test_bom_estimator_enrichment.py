"""Tests for provider-backed enrichment helpers."""

import pytest

from enrichment.providers import (  # pylint: disable=import-error
    LCSCAssemblyMetadataProvider,
    fetch_assembly_processes,
)

_EMPTY_METADATA = {
    "assembly_process": "",
    "component_product_type": None,
    "is_standard_assembly": False,
}


class _FakeApi:
    def __init__(self, product_type=2):
        self.product_type = product_type

    def get_part_data(self, lcsc):
        if lcsc == "C1":
            return {
                "success": True,
                "data": {
                    "data": {
                        "assemblyProcess": "SMT",
                        "componentProductType": self.product_type,
                    }
                },
            }
        return {"success": False}


def test_fetch_assembly_processes_uses_provider_contract():
    """Provider-backed helper preserves normalized payload contract."""
    result = fetch_assembly_processes(
        ["C1", "C2"], provider=LCSCAssemblyMetadataProvider(api=_FakeApi())
    )
    assert result == {
        "C1": {
            "assembly_process": "SMT",
            "component_product_type": 2,
            "is_standard_assembly": True,
        },
        "C2": _EMPTY_METADATA,
    }


class _RaisingApi:
    """API stub whose get_part_data always raises."""

    def get_part_data(self, code):
        raise RuntimeError(f"network down for {code}")


def test_normalize_returns_empty_metadata_when_api_raises():
    """API failures are swallowed; provider returns the empty metadata shape."""
    provider = LCSCAssemblyMetadataProvider(api=_RaisingApi())
    metadata = provider._normalize("C123")

    assert metadata == _EMPTY_METADATA


def test_fetch_iter_yields_empty_metadata_for_each_failed_code():
    """fetch_iter still yields one entry per code when the API raises."""
    provider = LCSCAssemblyMetadataProvider(api=_RaisingApi())
    results = dict(provider.fetch_iter(["C1", "C2"]))

    assert set(results) == {"C1", "C2"}
    for value in results.values():
        assert value == _EMPTY_METADATA


@pytest.mark.parametrize(
    ("product_type", "expected_type", "expected_standard"),
    [
        (1, 1, False),
        ("bad", None, False),
    ],
)
def test_normalize_uses_shared_product_type_classifier(
    product_type, expected_type, expected_standard
):
    """Representative values verify provider-to-classifier integration."""
    provider = LCSCAssemblyMetadataProvider(api=_FakeApi(product_type))

    metadata = provider._normalize("C1")

    assert metadata["component_product_type"] == expected_type
    assert metadata["is_standard_assembly"] is expected_standard
