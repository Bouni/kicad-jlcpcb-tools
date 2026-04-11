"""Tests for reusable dataview highlighting helpers."""

from dataview_highlight import (
    HighlightQueryCache,
    decode_highlighted_value,
    encode_highlighted_value,
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
