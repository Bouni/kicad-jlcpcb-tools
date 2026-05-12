"""Tests for provider-backed enrichment helpers."""

from enrichment.providers import (  # pylint: disable=import-error
    LCSCAssemblyMetadataProvider,
    fetch_assembly_processes,
)


class _FakeApi:
    def get_part_data(self, lcsc):
        if lcsc == "C1":
            return {
                "success": True,
                "data": {
                    "data": {
                        "assemblyProcess": "SMT",
                        "componentProductType": 2,
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
        "C2": {
            "assembly_process": "",
            "component_product_type": None,
            "is_standard_assembly": False,
        },
    }


class _RaisingApi:
    """API stub whose get_part_data always raises."""

    def get_part_data(self, code):
        raise RuntimeError(f"network down for {code}")


def test_normalize_returns_empty_metadata_when_api_raises():
    """API failures are swallowed; provider returns the empty metadata shape."""
    provider = LCSCAssemblyMetadataProvider(api=_RaisingApi())
    metadata = provider._normalize("C123")

    assert metadata == {
        "assembly_process": "",
        "component_product_type": None,
        "is_standard_assembly": False,
    }


def test_fetch_iter_yields_empty_metadata_for_each_failed_code():
    """fetch_iter still yields one entry per code when the API raises."""
    provider = LCSCAssemblyMetadataProvider(api=_RaisingApi())
    results = dict(provider.fetch_iter(["C1", "C2"]))

    assert set(results) == {"C1", "C2"}
    for value in results.values():
        assert value == {
            "assembly_process": "",
            "component_product_type": None,
            "is_standard_assembly": False,
        }
