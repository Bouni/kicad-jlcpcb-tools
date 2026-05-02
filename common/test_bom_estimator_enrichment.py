"""Tests for provider-backed enrichment helpers."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

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
