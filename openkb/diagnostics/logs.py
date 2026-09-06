"""Fail-closed Application Log tails for user-reviewed Diagnostic Bundles."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from openkb.diagnostics.handler import sanitize_application_log_line
from openkb.diagnostics.logging import (
    desktop_application_log_directory,
    flush_desktop_engine_logging,
)

MAX_BUNDLE_LOG_BYTES_PER_PROCESS = 10 * 1024 * 1024


def diagnostic_log_payloads(log_directory: Path | None = None) -> dict[str, bytes]:
    flush_desktop_engine_logging()
    directory = log_directory or desktop_application_log_directory()
    return {
        "application-logs/openkb-engine.jsonl": _log_tail(
            directory / "openkb-engine.log", "engine"
        ),
        "application-logs/openkb-shell.jsonl": _log_tail(directory / "openkb-shell.log", "shell"),
    }


def _log_tail(path: Path, process: str) -> bytes:
    lines: deque[bytes] = deque()
    total = 0
    candidates = tuple(Path(f"{path}.{index}") for index in range(4, 0, -1)) + (path,)
    for candidate in candidates:
        try:
            with candidate.open("rb") as stream:
                for raw_line in stream:
                    sanitized = sanitize_application_log_line(
                        raw_line.rstrip(b"\r\n"), expected_process=process
                    )
                    if sanitized is None:
                        continue
                    lines.append(sanitized)
                    total += len(sanitized)
                    while total > MAX_BUNDLE_LOG_BYTES_PER_PROCESS and lines:
                        total -= len(lines.popleft())
        except OSError:
            continue
    return b"".join(lines)
