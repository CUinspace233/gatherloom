"""Chinese-aware tokenization for the keyword index (ADR 0005).

Text is segmented with jieba's search mode, lowercased and joined with spaces,
then stored with PostgreSQL's ``simple`` configuration. Queries must use the
same function so that both sides agree on word boundaries.
"""

import logging
import re

import jieba

jieba.setLogLevel(logging.WARNING)

TOKENIZER_VERSION = 1
_WORD = re.compile(r"\w")


def tokenize(text: str) -> list[str]:
    """Return search tokens: words and their sub-words, without punctuation."""
    return [
        token.lower()
        for token in (piece.strip() for piece in jieba.cut_for_search(text))
        if token and _WORD.search(token)
    ]


def index_text(text: str) -> str:
    """Space-joined tokens, ready for ``to_tsvector('simple', ...)``."""
    return " ".join(tokenize(text))


def warm_up() -> None:
    """Load the dictionary once, before worker threads start."""
    jieba.initialize()
