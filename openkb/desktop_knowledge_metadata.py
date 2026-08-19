"""Small codecs for controlled Knowledge Analysis metadata."""

from __future__ import annotations

import json


def encode_knowledge_labels(values: tuple[str, ...]) -> str:
    """Serialize validated aliases or tags for SQLite authority rows."""
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def decode_knowledge_labels(value: object) -> tuple[str, ...]:
    """Decode a stored label array without trusting malformed projection data."""
    if value is None:
        return ()
    try:
        payload = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        return ()
    return tuple(str(item) for item in payload)
