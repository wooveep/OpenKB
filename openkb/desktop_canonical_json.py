"""One canonical JSON serialization and digest policy for Desktop contracts."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    """Serialize a JSON-compatible value without locale or whitespace variance."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json_digest(value: object) -> str:
    """Hash the UTF-8 canonical JSON representation of a value."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
