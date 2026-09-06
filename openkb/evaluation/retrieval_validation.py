"""Primitive JSON boundary validation for retrieval evaluation contracts."""

from __future__ import annotations


def required_string(value: dict[object, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"Desktop retrieval evaluation field {key} must be a string.")
    return candidate.strip()


def required_strings(value: dict[object, object], key: str) -> tuple[str, ...]:
    candidates = value.get(key, [])
    if not isinstance(candidates, list):
        raise ValueError(f"Desktop retrieval evaluation field {key} must be an array.")
    values = tuple(item.strip() for item in candidates if isinstance(item, str) and item.strip())
    if len(values) != len(candidates):
        raise ValueError(f"Desktop retrieval evaluation field {key} contains invalid values.")
    return values


def optional_string(value: dict[object, object], key: str) -> str | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"Desktop retrieval evaluation field {key} must be a string or null.")
    return candidate.strip()


def optional_sha256(value: dict[object, object], key: str) -> str | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if (
        not isinstance(candidate, str)
        or len(candidate) != 64
        or any(character not in "0123456789abcdef" for character in candidate)
    ):
        raise ValueError(f"Desktop retrieval evaluation field {key} must be a SHA-256.")
    return candidate


def required_sha256(value: dict[object, object], key: str) -> str:
    candidate = optional_sha256(value, key)
    if candidate is None:
        raise ValueError(f"Desktop retrieval evaluation field {key} must be a SHA-256.")
    return candidate


def optional_positive_float(value: dict[object, object], key: str) -> float | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or candidate <= 0:
        raise ValueError(f"Desktop retrieval evaluation field {key} must be positive.")
    return float(candidate)


def optional_nonnegative_int(value: dict[object, object], key: str) -> int | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        raise ValueError(f"Desktop retrieval evaluation field {key} must be nonnegative.")
    return candidate


def report_int(value: dict[object, object], key: str, *, minimum: int = 0) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < minimum:
        raise ValueError(f"Desktop retrieval evaluation report field {key} is invalid.")
    return candidate


def report_float(value: dict[object, object], key: str) -> float:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or candidate < 0:
        raise ValueError(f"Desktop retrieval evaluation report field {key} is invalid.")
    return float(candidate)


def optional_report_float(value: dict[object, object], key: str, default: float) -> float:
    return report_float(value, key) if key in value else default


def report_bool(value: dict[object, object], key: str) -> bool:
    candidate = value.get(key)
    if not isinstance(candidate, bool):
        raise ValueError(f"Desktop retrieval evaluation report field {key} is invalid.")
    return candidate


def optional_report_bool(value: dict[object, object], key: str) -> bool:
    return report_bool(value, key) if key in value else False
