"""Tests for the SQL escaping helpers in search_escape.py."""

# ruff: noqa: D102

from pathlib import Path
import sqlite3
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from search_escape import (
    escape_fts_phrase as _escape_fts_phrase,
    escape_like_term as _escape_like_term,
)

# ---------------------------------------------------------------------------
# _escape_like_term
# ---------------------------------------------------------------------------


class TestEscapeLikeTerm:
    """Unit tests for _escape_like_term."""

    def test_percent_is_escaped(self):
        assert _escape_like_term("1%") == "1\\%"

    def test_underscore_is_escaped(self):
        assert _escape_like_term("1_") == "1\\_"

    def test_backslash_is_escaped(self):
        assert _escape_like_term("a\\b") == "a\\\\b"

    def test_single_quote_is_escaped(self):
        assert _escape_like_term("it's") == "it''s"

    def test_plain_term_unchanged(self):
        assert _escape_like_term("10k") == "10k"

    def test_multiple_special_chars(self):
        assert _escape_like_term("%_50%") == "\\%\\_50\\%"

    # Integration: verify SQLite actually honours the escaped LIKE pattern.

    def _like_matches(self, haystack: str, needle: str) -> bool:
        """Return True if haystack matches the escaped LIKE pattern for needle."""
        escaped = _escape_like_term(needle)
        sql = f"SELECT 1 WHERE '{haystack}' LIKE '%{escaped}%' ESCAPE '\\'"
        return sqlite3.connect(":memory:").execute(sql).fetchone() is not None

    def test_percent_matches_literally_not_as_wildcard(self):
        # "1%" must appear as a literal substring — '1' immediately followed by '%'
        assert self._like_matches("1% tolerance", "1%") is True  # contains "1%"
        assert self._like_matches("1x tolerance", "1%") is False  # no literal '%'
        assert self._like_matches("10%", "1%") is False  # '1' and '%' not adjacent

    def test_underscore_matches_literally_not_as_wildcard(self):
        assert self._like_matches("1_", "1_") is True
        assert self._like_matches("1x", "1_") is False

    def test_plain_substring_still_matches(self):
        assert self._like_matches("10k resistor", "10") is True

    def test_no_match_on_unrelated_string(self):
        assert self._like_matches("100n capacitor", "1%") is False


# ---------------------------------------------------------------------------
# _escape_fts_phrase
# ---------------------------------------------------------------------------


class TestEscapeFtsPhrase:
    """Unit tests for _escape_fts_phrase."""

    def test_double_quote_is_doubled(self):
        assert _escape_fts_phrase('say "hello"') == 'say ""hello""'

    def test_single_quote_is_doubled(self):
        assert _escape_fts_phrase("it's") == "it''s"

    def test_both_quote_types(self):
        assert _escape_fts_phrase('"it\'s"') == '""it\'\'s""'

    def test_plain_term_unchanged(self):
        assert _escape_fts_phrase("resistor") == "resistor"

    def test_empty_string(self):
        assert _escape_fts_phrase("") == ""

    # Integration: verify the escaped phrase embeds safely in a MATCH string.

    def test_double_quote_in_fts_match_does_not_crash(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(description)")
        con.execute("INSERT INTO t VALUES ('say hello world')")
        con.execute("INSERT INTO t VALUES ('say \"hello\" world')")
        escaped = _escape_fts_phrase('say "hello"')
        # Should not raise; FTS5 phrase with escaped double-quote
        rows = con.execute(
            f"SELECT description FROM t WHERE t MATCH '\"{escaped}\"'"
        ).fetchall()
        assert any('"hello"' in r[0] for r in rows)
