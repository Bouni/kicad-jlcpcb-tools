"""Supplier facts follow native assignments without per-reference persistence."""

from pathlib import Path
from types import ModuleType

from .store_board_support import (
    Board,
    Footprint,
    make_store,
    store_module as _store_module,
)

store_module = _store_module


def test_reassigned_batch_receives_only_its_current_supplier_facts(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Late results cannot classify changed IDs and current equal IDs share facts."""
    first, second, third = Footprint("R1"), Footprint("R2"), Footprint("R3", "C300")
    board = Board(first, second, third)
    store = make_store(store_module, tmp_path, board)
    assert store.get_assembly_enrichment_targets() == {
        "C100": ["R1", "R2"],
        "C300": ["R3"],
    }
    first.lcsc = "C200"
    assert not store.set_assembly_metadata("R1", "THT", 2, expected_lcsc="C100")
    assert store.set_assembly_metadata("R2", "SMT", 0, expected_lcsc="C100")
    assert store.get_part("R1")["component_product_type"] is None
    assert store.get_part("R2")["component_product_type"] == 0
    first.lcsc = "C100"
    assert store.get_part("R1")["component_product_type"] == 0
    second.lcsc = ""
    assert store.get_part("R2")["component_product_type"] is None
    assert not store.set_assembly_metadata("R2", "SMT", 0, expected_lcsc="C100")
    board.footprints.remove(first)
    assert not store.set_assembly_metadata("R1", "SMT", 0, expected_lcsc="C100")
    assert store.get_part("R3")["component_product_type"] is None
    assert store.get_assembly_enrichment_targets([]) == {}
    assert store.get_assembly_enrichment_targets(["R3"]) == {"C300": ["R3"]}
    assert not (tmp_path / "jlcpcb").exists()


def test_supplier_result_before_assignment_is_shared_only_with_matching_code(
    tmp_path: Path, store_module: ModuleType
) -> None:
    """Per-code facts can outlive subscribers without contaminating another ID."""
    footprint = Footprint(lcsc="")
    store = make_store(store_module, tmp_path, Board(footprint))
    store.cache_lcsc_metadata(" c100 ", "SMT", 2)
    footprint.lcsc = "C200"
    assert store.get_part("R1")["component_product_type"] is None
    footprint.lcsc = "C100"
    assert store.get_part("R1")["component_product_type"] == 2
    reopened = make_store(store_module, tmp_path, store.board)
    assert reopened.get_part("R1")["component_product_type"] is None
    assert reopened.get_assembly_enrichment_targets() == {"C100": ["R1"]}
