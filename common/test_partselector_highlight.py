"""Tests for part selector text highlight helpers."""

# ruff: noqa: D103

from partselector_highlight import (
    cached_highlight_spans,
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


def test_cached_highlight_spans_stores_negative_results():
    cache: dict[str, list[tuple[int, int]]] = {}
    spans = cached_highlight_spans(cache, "Murata Electronics", ["zzzz"])
    assert spans == []
    assert cache["Murata Electronics"] == []


def test_cached_highlight_spans_reuses_cached_negative_results():
    cache: dict[str, list[tuple[int, int]]] = {"Murata Electronics": []}
    spans = cached_highlight_spans(cache, "Murata Electronics", ["murata"])
    assert spans == []
