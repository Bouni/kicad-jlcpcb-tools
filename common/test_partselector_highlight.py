"""Tests for reusable dataview highlighting helpers."""

from dataview_highlight import (
    HighlightQueryCache,
    decode_highlighted_value,
    encode_highlighted_value,
    expand_footprint,
    expand_value,
    filtered_highlight_terms,
    find_highlight_spans,
    normalize_highlight_terms,
    simplify_footprint_name,
)


def test_normalize_highlight_terms_deduplicates_casefolded_terms():
    """Normalization folds case and removes duplicate query terms."""
    assert normalize_highlight_terms(" 10K 0603 10k ") == ["10k", "0603"]


def test_filtered_highlight_terms_skips_short_terms():
    """Single-character terms are ignored to avoid noisy broad highlights."""
    assert filtered_highlight_terms("1 12 1206") == ["12", "1206"]


def test_find_highlight_spans_merges_overlaps():
    """Highlight spans are returned in display order without overlap duplication."""
    assert find_highlight_spans("0603 10kΩ", ["0603", "10k"]) == [(0, 4), (5, 8)]


def test_find_highlight_spans_prefers_longest_overlap_when_merged():
    """Overlapping matches merge to the longest highlighted span."""
    terms = normalize_highlight_terms("10K 10KΩ")
    assert find_highlight_spans("10KΩ", terms) == [(0, 4)]


def test_highlight_query_cache_stores_negative_results():
    """Cache stores empty span lookups so misses are reused efficiently."""
    cache = HighlightQueryCache()
    cache.prepare("zzzz")

    assert cache.get_spans("Murata Electronics") == []


def test_highlight_query_cache_reuses_cached_negative_results():
    """Repeated misses return the cached list instance for the same text."""
    cache = HighlightQueryCache()
    cache.prepare("zzzz")

    first = cache.get_spans("Murata Electronics")
    second = cache.get_spans("Murata Electronics")

    assert first == []
    assert second == []
    assert first is second


def test_highlight_query_cache_prepare_changes_terms_and_resets_spans():
    """Changing query text resets cached spans and prepared terms."""
    cache = HighlightQueryCache()
    cache.prepare("murata")
    spans_before = cache.get_spans("Murata Electronics")

    cache.prepare("1206")
    spans_after = cache.get_spans("Murata Electronics")

    assert spans_before != []
    assert cache.get_terms() == ["1206"]
    assert spans_after == []


def test_highlight_query_cache_clear_resets_all_state():
    """Clearing the cache drops both prepared terms and computed spans."""
    cache = HighlightQueryCache()
    cache.prepare("murata 1206")
    _ = cache.get_spans("Murata Electronics")

    cache.clear()

    assert cache.get_terms() == []


def test_encode_decode_highlighted_value_round_trip():
    """Packed cell values preserve display text and normalized highlight terms."""
    encoded = encode_highlighted_value("10kΩ 1% 0603", ["10kΩ", "0603"])

    assert decode_highlighted_value(encoded) == (
        "10kΩ 1% 0603",
        ["10kω", "0603"],
    )


def test_decode_highlighted_value_without_metadata_returns_plain_text():
    """Plain strings decode to themselves with no highlight metadata."""
    assert decode_highlighted_value("plain text") == ("plain text", [])


def test_simplify_footprint_name_extracts_metric_size():
    """Metric KiCad footprints are reduced to the expected short package name."""
    assert simplify_footprint_name("Resistor_SMD:R_0603_1608Metric") == "0603"


def test_simplify_footprint_name_falls_back_to_last_token():
    """Non-metric footprint names fall back to a readable final token."""
    assert simplify_footprint_name("Package_TO_SOT_SMD:SOT-23") == "SOT-23"


def test_expand_value_for_resistor_adds_ohm_symbol_variants():
    """Resistor values expand with Ω-compatible alternatives for matching."""
    assert expand_value("R1", "390R") == ["390R", "390Ω"]
    assert expand_value("R2", "10K") == ["10K", "10KΩ"]
    assert expand_value("R3", "10KΩ") == ["10KΩ", "10K"]


def test_expand_value_for_non_resistor_keeps_original_only():
    """Non-resistor references do not get resistor-specific value expansion."""
    assert expand_value("C1", "10K") == ["10K"]


def test_expand_value_for_capacitor_adds_micro_variants():
    """Capacitor values add interchangeable `u` and `µ` variants."""
    assert expand_value("C10", "10µF") == ["10µF", "10uF", "10µ", "10u"]
    assert expand_value("C11", "10uF") == ["10uF", "10µF", "10u", "10µ"]


def test_expand_value_for_capacitor_without_suffix_adds_f_variants():
    """Capacitor values without `F` also get `F`-suffixed variants."""
    assert expand_value("C12", "1u") == ["1u", "1µ", "1uF", "1µF"]
    assert expand_value("C13", "1µ") == ["1µ", "1u", "1µF", "1uF"]


def test_expand_footprint_adds_known_aliases():
    """Footprint aliases include known compatible package naming alternatives."""
    assert "SO-8" in expand_footprint("U1", "Package_SO:SIOC-8")
    assert "TO-236" in expand_footprint("Q1", "Package_TO_SOT_SMD:SOT-23")


def test_expand_footprint_adds_reverse_aliases_generated_from_forward_map():
    """Reverse aliases are auto-generated from the canonical alias map."""
    assert "SIOC-8" in expand_footprint("U2", "Package_SO:SO-8")
    assert "SOT-23" in expand_footprint("Q2", "Package_TO_SOT_SMD:TO-236")


def test_expand_footprint_maps_capacitor_electrolytic_diameter():
    """Capacitor electrolytic footprints emit an SMD diameter term for matching."""
    assert "SMD,D6.3" in expand_footprint("C5", "Capacitor_SMD:CP_Elec_6.3x7.7")


def test_expand_footprint_diameter_mapping_is_capacitor_only():
    """Electrolytic diameter mapping is only applied for capacitor references."""
    assert "SMD,D6.3" not in expand_footprint("U5", "Capacitor_SMD:CP_Elec_6.3x7.7")
