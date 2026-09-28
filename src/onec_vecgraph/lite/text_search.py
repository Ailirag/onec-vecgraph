"""Shared lexical-query helpers for the lightweight SQLite search indexes.

The lite server deliberately has no morphological/ML dependency.  These helpers keep the
small amount of Russian query normalisation identical for working-copy and platform-help
search: conservative question-word removal plus the existing two-character prefix fallback.
"""

from __future__ import annotations

import re

from ..chunking import search_tokens

_CYR = re.compile(r"[а-яё]", re.IGNORECASE)

# Only words which carry virtually no retrieval intent are removed.  Domain words such as
# «объект», «значение», «форма» deliberately stay: in 1C they are useful discriminators.
_STOPWORDS = frozenset({
    "а", "бы", "в", "во", "где", "для", "и", "из", "или", "как", "к", "ко", "на", "над",
    "но", "о", "об", "по", "под", "при", "про", "с", "со", "у", "чтобы", "это", "этот", "эта", "эти",
})


def query_terms(query: str, *, drop_stopwords: bool = True) -> list[str]:
    """Tokenise a user query like identifiers are tokenised at index time.

    The original CamelCase identifier and its components are retained by ``search_tokens``.
    Duplicates are removed case-insensitively while preserving order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in search_tokens(query).split():
        low = token.lower()
        if not low or (drop_stopwords and low in _STOPWORDS) or low in seen:
            continue
        seen.add(low)
        out.append(low)
    return out


def fts_query(query: str) -> str:
    """Convert user text to a safe FTS5 OR query.

    Russian words of six or more characters get a conservative prefix alternative, matching
    common case/number endings without a stemmer (``значению`` -> ``значен*``).
    """
    terms = query_terms(query)
    if not terms:  # A query made only of stop words is still searchable, not silently empty.
        terms = query_terms(query, drop_stopwords=False)
    parts: list[str] = []
    for term in terms:
        safe = term.replace('"', " ").strip()
        if not safe:
            continue
        if _CYR.search(safe) and len(safe) >= 6:
            parts.append(f'("{safe}" OR "{safe[:-2]}"*)')
        else:
            parts.append(f'"{safe}"')
    return " OR ".join(parts)


def term_coverage(terms: list[str], *texts: str | None) -> int:
    """How many query terms occur in the candidate, with the same Russian-prefix fallback."""
    if not terms:
        return 0
    candidate = {t.lower() for t in search_tokens(*texts).split()}
    covered = 0
    for term in terms:
        if term in candidate:
            covered += 1
            continue
        if _CYR.search(term) and len(term) >= 6:
            prefix = term[:-2]
            if any(word.startswith(prefix) for word in candidate):
                covered += 1
    return covered
