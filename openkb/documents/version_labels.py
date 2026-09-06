"""Pure parsing and ordering rules for user-confirmed Document Version labels."""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

VersionScheme = Literal["numeric_dotted", "semver", "calendar", "opaque"]

_EXPLICIT_PREFIX = re.compile(r"(?i)^\s*(?:version|ver|版本|v)\s*[-_:：]?\s*")
_NUMERIC_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CALENDAR = re.compile(r"^(\d{4})[-._](\d{1,2})[-._](\d{1,2})$")
_EXPLICIT_IN_TEXT = re.compile(
    r"(?i)(?:\bversion\s*|\bver\.?\s*|\bv\s*|版本\s*)"
    r"([0-9]+(?:\.[0-9A-Za-z-]+){0,3}(?:-[0-9A-Za-z.-]+)?)(?![0-9A-Za-z])"
)


@dataclass(frozen=True)
class ParsedVersionLabel:
    raw_label: str
    normalized_label: str
    scheme: VersionScheme
    key_json: str
    order_key: tuple[object, ...] | None


def normalize_version_label(label: str) -> str:
    value = unicodedata.normalize("NFKC", label).strip()
    value = _EXPLICIT_PREFIX.sub("", value)
    return "".join(value.casefold().split())


def parse_version_label(
    label: str,
    scheme: VersionScheme,
    *,
    explicit_signal: bool = True,
) -> ParsedVersionLabel | None:
    """Parse only under an explicit signal; never guess versions from bare numbers."""
    if not isinstance(label, str) or not label.strip() or not explicit_signal:
        return None
    normalized = normalize_version_label(label)
    if not normalized:
        return None
    key: tuple[object, ...] | None
    if scheme == "numeric_dotted":
        match = _NUMERIC_DOTTED.fullmatch(normalized)
        if match is None:
            return None
        parts = tuple(int(value) for value in match.group(1).split("."))
        key = parts
        payload: object = {"segments": parts}
    elif scheme == "semver":
        match = _SEMVER.fullmatch(normalized)
        if match is None:
            return None
        prerelease = _semver_prerelease(match.group(4))
        key = (int(match.group(1)), int(match.group(2)), int(match.group(3)), *prerelease)
        payload = {
            "major": int(match.group(1)),
            "minor": int(match.group(2)),
            "patch": int(match.group(3)),
            "prerelease": match.group(4) or "",
        }
    elif scheme == "calendar":
        match = _CALENDAR.fullmatch(normalized)
        if match is None:
            return None
        try:
            value = dt.date(*(int(match.group(index)) for index in range(1, 4)))
        except ValueError:
            return None
        key = (value.year, value.month, value.day)
        payload = {"year": value.year, "month": value.month, "day": value.day}
    else:
        key = None
        payload = {"opaque": normalized}
    return ParsedVersionLabel(
        raw_label=label.strip(),
        normalized_label=normalized,
        scheme=scheme,
        key_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        order_key=key,
    )


def compare_version_labels(left: str, right: str, scheme: VersionScheme) -> int | None:
    """Return ordering only when the selected scheme proves comparability."""
    parsed_left = parse_version_label(left, scheme)
    parsed_right = parse_version_label(right, scheme)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left.normalized_label == parsed_right.normalized_label:
        return 0
    if parsed_left.order_key is None or parsed_right.order_key is None:
        return None
    return -1 if parsed_left.order_key < parsed_right.order_key else 1


def version_label_candidates(value: str, *, known_labels: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Extract explicit labels while rejecting IPs, ports, decimals, and model numbers."""
    normalized_known = {
        normalize_version_label(label): label for label in known_labels if label.strip()
    }
    results: list[str] = []
    for match in _EXPLICIT_IN_TEXT.finditer(unicodedata.normalize("NFKC", value)):
        candidate = match.group(1).rstrip(".,;:，。；：")
        normalized = normalize_version_label(candidate)
        if _looks_like_ipv4(normalized):
            continue
        rendered = normalized_known.get(normalized, candidate)
        if rendered not in results:
            results.append(rendered)
    for normalized, original in normalized_known.items():
        if normalized and normalized in normalize_version_label(value) and original not in results:
            results.append(original)
    return tuple(results)


def _semver_prerelease(value: str | None) -> tuple[object, ...]:
    if value is None:
        return (1,)
    parts: list[object] = [0]
    for part in value.split("."):
        if part.isdigit():
            parts.extend((0, int(part)))
        else:
            parts.extend((1, part.casefold()))
    return tuple(parts)


def _looks_like_ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)
