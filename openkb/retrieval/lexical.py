"""Small deterministic lexical helpers shared by Desktop retrieval features."""

from __future__ import annotations


def is_cjk_text(value: str) -> bool:
    """Return whether a token consists entirely of the CJK range we tokenize by bigram."""
    return bool(value) and all("\u3400" <= character <= "\u9fff" for character in value)


def cjk_bigrams(value: str) -> tuple[str, ...]:
    """Preserve short CJK tokens and split longer tokens into overlapping bigrams."""
    if len(value) <= 2:
        return (value,)
    return tuple(value[index : index + 2] for index in range(len(value) - 1))
