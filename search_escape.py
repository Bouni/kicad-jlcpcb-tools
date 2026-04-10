"""SQL escaping helpers for the parts library search."""


def escape_like_term(term: str) -> str:
    r"""Escape a term for use inside a SQL LIKE expression.

    Uses backslash as the escape character, so the caller must append
    ``ESCAPE '\'`` to the LIKE predicate.  Also escapes embedded single
    quotes so the term is safe to inline into a SQL string literal.
    """
    return (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("'", "''")
    )


def escape_fts_phrase(term: str) -> str:
    r"""Escape a term for use inside an FTS5 double-quoted phrase token.

    Inside ``\"...\"`` phrase tokens a literal ``\"`` must be doubled.
    The whole MATCH expression is embedded in a SQL ``'...'`` string so
    a literal ``'`` must also be escaped as ``''``.
    """
    return term.replace('"', '""').replace("'", "''")
