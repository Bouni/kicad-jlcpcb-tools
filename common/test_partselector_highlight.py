"""Tests for part selector text highlight helpers."""

# ruff: noqa: D103

from partselector_highlight import (
    HighlightQueryCache,
    filtered_highlight_terms,
    find_highlight_spans,
    normalize_highlight_terms,
)


def test_normalize_highlight_terms_splits_and_deduplicates():
    assert normalize_highlight_terms(" 10k %0603% 10K ") == ["10k", "0603"]


def test_find_highlight_spans_matches_multiple_terms_case_insensitively():
    assert find_highlight_spans("10K resistor 0603", ["10k", "0603"]) == [
        (0, 3),
        (13, 17),
    ]


def test_find_highlight_spans_merges_overlapping_matches():
    assert find_highlight_spans("abcde", ["abc", "bcd"]) == [(0, 4)]


def test_filtered_highlight_terms_skips_short_terms():
    assert filtered_highlight_terms("1 12 1206") == ["12", "1206"]


def test_highlight_query_cache_stores_negative_results():
    cache = HighlightQueryCache()
    cache.prepare("zzzz")
    spans = cache.get_spans("Murata Electronics")
    assert spans == []


def test_highlight_query_cache_reuses_cached_negative_results():
    cache = HighlightQueryCache()
    cache.prepare("zzzz")
    first = cache.get_spans("Murata Electronics")
    second = cache.get_spans("Murata Electronics")

    assert first == []
    assert second == []
    assert first is second


def test_highlight_query_cache_prepare_changes_terms_and_resets_spans():
    cache = HighlightQueryCache()
    cache.prepare("murata")
    spans_before = cache.get_spans("Murata Electronics")
    cache.prepare("1206")
    spans_after = cache.get_spans("Murata Electronics")

    assert spans_before != []
    assert cache.get_terms() == ["1206"]
    assert spans_after == []


def test_highlight_query_cache_clear_resets_all_state():
    cache = HighlightQueryCache()
    cache.prepare("murata 1206")
    _ = cache.get_spans("Murata Electronics")
    cache.clear()

    assert cache.get_terms() == []
