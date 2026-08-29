"""Support-safe JSON Lines handler shared by Desktop Engine modules."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter, deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from openkb.desktop_logging_settings import (
    DIAGNOSTIC_COMPONENTS,
    TRACE_LEVEL,
    DiagnosticLoggingSettings,
    component_for_logger,
)

SCHEMA_VERSION = 1
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 4
LOW_PRIORITY_QUEUE_CAPACITY = 2_048
DEDUPLICATION_WINDOW_SECONDS = 60.0

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
_SAFE_FIELDS = frozenset(
    {
        "adapter",
        "attempt",
        "attempt_count",
        "batch_id",
        "bytes",
        "cache_hit",
        "call_id",
        "capability_identity",
        "capture_id",
        "component_levels",
        "configured_level",
        "document_count",
        "document_id",
        "dropped_debug",
        "dropped_info",
        "dropped_trace",
        "edge_count",
        "elapsed_ms",
        "effective_level",
        "endpoint",
        "error_code",
        "error_type",
        "failure_event_id",
        "failure_kind",
        "failure_signature",
        "failure_stage",
        "final_character_count",
        "final_chunk_count",
        "final_content_observed",
        "finish_reason",
        "input_tokens",
        "issue_codes",
        "issue_count",
        "issue_dispositions",
        "issue_failure_classes",
        "issue_paths",
        "issues_truncated",
        "job_id",
        "knowledge_base_id",
        "lane",
        "last_completed_stage",
        "level",
        "method",
        "model",
        "model_role",
        "next_action",
        "node_count",
        "operation",
        "outcome",
        "output_tokens",
        "output_limit_reached",
        "parent_operation",
        "parent_prompt_contract_digest",
        "parser",
        "path_kind",
        "phase",
        "process_exit_code",
        "prompt_contract_digest",
        "provider",
        "provider_error_code",
        "provider_request_id",
        "queue_capacity",
        "reasoning_character_count",
        "reasoning_chunk_count",
        "reasoning_observed",
        "recovery_action",
        "rejected_count",
        "retained_count",
        "repair_attempted",
        "result_id",
        "result_quality",
        "result_status",
        "request_id",
        "retry_after_seconds",
        "retryable",
        "schema_name",
        "source_extension",
        "source_size_bytes",
        "stage",
        "stage_run_id",
        "status",
        "stop_reason",
        "streaming",
        "suppressed_count",
        "timeout_seconds",
        "total_tokens",
        "trace_components",
        "validation_error_code",
        "warning_code",
        "weakened_count",
        "worker_id",
    }
)


def _canonical_level_name(levelno: int) -> str:
    if levelno <= TRACE_LEVEL:
        return "TRACE"
    if levelno <= logging.DEBUG:
        return "DEBUG"
    if levelno <= logging.INFO:
        return "INFO"
    if levelno <= logging.WARNING:
        return "WARN"
    return "ERROR"


def _safe_endpoint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.hostname is None:
        return None
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}"


def _safe_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:512]
    return None


def sanitize_event_fields(fields: Mapping[str, object] | None) -> dict[str, object]:
    """Keep only the explicitly support-safe diagnostic vocabulary."""
    sanitized: dict[str, object] = {}
    for key, value in (fields or {}).items():
        if key not in _SAFE_FIELDS:
            continue
        if key == "endpoint":
            endpoint = _safe_endpoint(value)
            if endpoint is not None:
                sanitized[key] = endpoint
            continue
        if isinstance(value, (list, tuple)):
            items = [_safe_scalar(item) for item in value]
            sanitized[key] = [item for item in items if item is not None]
            continue
        if isinstance(value, Mapping):
            nested = sanitize_event_fields(value)
            if nested:
                sanitized[key] = nested
            continue
        scalar = _safe_scalar(value)
        if scalar is not None:
            sanitized[key] = scalar
    return sanitized


def _sanitized_stack(record: logging.LogRecord) -> tuple[str | None, list[dict[str, object]]]:
    exc_info = record.exc_info
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not isinstance(exc_info, tuple) or len(exc_info) != 3:
        return None, []
    error_type = exc_info[0].__name__ if exc_info[0] is not None else None
    frames = traceback.extract_tb(exc_info[2]) if exc_info[2] else []
    return error_type, [
        {
            "module": Path(frame.filename).stem[:128],
            "function": frame.name[:128],
            "line": frame.lineno,
        }
        for frame in frames[-20:]
    ]


def _event_name(record: logging.LogRecord) -> str:
    explicit = getattr(record, "openkb_event", None)
    if isinstance(explicit, str) and _EVENT_PATTERN.fullmatch(explicit):
        return explicit
    template = record.msg if isinstance(record.msg, str) else ""
    candidate = template.split(maxsplit=1)[0].strip(".:,").lower()
    if _EVENT_PATTERN.fullmatch(candidate) and any(
        separator in candidate for separator in ("_", ".", "-")
    ):
        return candidate
    module = re.sub(r"[^a-z0-9]+", "_", record.name.rsplit(".", 1)[-1].lower()).strip("_")
    function = re.sub(r"[^a-z0-9]+", "_", record.funcName.lower()).strip("_")
    fallback = f"legacy_{module or 'module'}_{function or 'call'}"[:96].rstrip("_")
    return fallback if _EVENT_PATTERN.fullmatch(fallback) else "legacy_log_record"


class DiagnosticJsonFormatter(logging.Formatter):
    """Format one event without ever interpolating legacy log arguments."""

    def __init__(self, settings: DiagnosticLoggingSettings) -> None:
        super().__init__()
        self._settings = settings

    def format(self, record: logging.LogRecord) -> str:
        event = _event_name(record)
        component = getattr(record, "openkb_component", None)
        if component not in DIAGNOSTIC_COMPONENTS:
            component = component_for_logger(record.name)
        summary = getattr(record, "openkb_summary", None)
        if not isinstance(summary, str):
            summary = event.replace("_", " ").capitalize() + "."
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        )
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": _canonical_level_name(record.levelno),
            "event": event,
            "summary": summary[:512],
            "runtime_session_id": self._settings.runtime_session_id,
            "process": "engine",
            "pid": os.getpid(),
            "thread": (record.threadName or "unknown")[:128],
            "component": component,
            "sequence": int(getattr(record, "openkb_sequence", 0)),
        }
        fields = getattr(record, "openkb_fields", None)
        if isinstance(fields, Mapping):
            payload.update(sanitize_event_fields(fields))
        error_type, stack = _sanitized_stack(record)
        if error_type is not None:
            payload["error_type"] = error_type
        if stack:
            payload["stack"] = stack
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _BoundedEventQueue:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._items: deque[logging.LogRecord] = deque()
        self._condition = threading.Condition()
        self._unfinished = 0
        self._closed = False

    def put(self, record: logging.LogRecord) -> int | None:
        """Return the level of a dropped record, preferring TRACE then DEBUG."""
        with self._condition:
            if self._closed:
                return record.levelno
            if len(self._items) < self._capacity:
                self._items.append(record)
                self._unfinished += 1
                self._condition.notify()
                return None
            replace_index: int | None = None
            for preferred_level in (TRACE_LEVEL, logging.DEBUG):
                if record.levelno <= preferred_level:
                    break
                replace_index = next(
                    (
                        index
                        for index, queued in enumerate(self._items)
                        if queued.levelno <= preferred_level
                    ),
                    None,
                )
                if replace_index is not None:
                    break
            if replace_index is None:
                return record.levelno
            dropped = self._items[replace_index]
            del self._items[replace_index]
            self._items.append(record)
            self._condition.notify()
            return dropped.levelno

    def get(self) -> logging.LogRecord | None:
        with self._condition:
            while not self._items and not self._closed:
                self._condition.wait()
            if not self._items:
                return None
            return self._items.popleft()

    def task_done(self) -> None:
        with self._condition:
            self._unfinished -= 1
            if self._unfinished == 0:
                self._condition.notify_all()

    def join(self) -> None:
        with self._condition:
            while self._unfinished:
                self._condition.wait()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class HybridDiagnosticHandler(logging.Handler):
    """Queue lower-level events while flushing WARN/ERROR synchronously."""

    _openkb_desktop_engine_log = True

    def __init__(
        self,
        path: Path,
        settings: DiagnosticLoggingSettings,
        *,
        queue_capacity: int = LOW_PRIORITY_QUEUE_CAPACITY,
    ) -> None:
        super().__init__(TRACE_LEVEL)
        self.settings = settings
        self.path = path
        self._target = RotatingFileHandler(
            path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        self._target.setFormatter(DiagnosticJsonFormatter(settings))
        self._queue = _BoundedEventQueue(queue_capacity)
        self._write_lock = threading.Lock()
        self._sequence = 0
        self._drop_counts: Counter[str] = Counter()
        self._dedupe: dict[tuple[object, ...], tuple[float, int]] = {}
        self._closed = False
        self._worker = threading.Thread(
            target=self._run_worker,
            name="openkb-diagnostic-log-writer",
            daemon=True,
        )
        self._worker.start()

    def _component(self, record: logging.LogRecord) -> str:
        explicit = getattr(record, "openkb_component", None)
        return explicit if isinstance(explicit, str) else component_for_logger(record.name)

    def _enabled(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.settings.effective_level(self._component(record))

    def _should_suppress(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "openkb_dedupe", False):
            return False
        if getattr(record, "openkb_terminal", False) or record.levelno >= logging.WARNING:
            return False
        fields = getattr(record, "openkb_fields", {})
        if not isinstance(fields, Mapping):
            fields = {}
        key = (
            _event_name(record),
            self._component(record),
            fields.get("method"),
            fields.get("job_id"),
            fields.get("call_id"),
            fields.get("error_code"),
        )
        now = time.monotonic()
        previous = self._dedupe.get(key)
        if previous is not None and now - previous[0] < DEDUPLICATION_WINDOW_SECONDS:
            self._dedupe[key] = (previous[0], previous[1] + 1)
            return True
        suppressed = previous[1] if previous is not None else 0
        if suppressed:
            copied = dict(fields)
            copied["suppressed_count"] = suppressed
            record.openkb_fields = copied  # type: ignore[attr-defined]
        self._dedupe[key] = (now, 0)
        return False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._closed or not self._enabled(record) or self._should_suppress(record):
                return
            if record.levelno >= logging.WARNING:
                self._write(record)
                return
            dropped_level = self._queue.put(record)
            if dropped_level is not None:
                self._drop_counts[_canonical_level_name(dropped_level).lower()] += 1
        except Exception:
            # Diagnostics must never alter a domain outcome.
            return

    def _run_worker(self) -> None:
        while (record := self._queue.get()) is not None:
            try:
                self._write(record)
            finally:
                self._queue.task_done()

    def _write(self, record: logging.LogRecord) -> None:
        with self._write_lock:
            if self._drop_counts:
                counts = self._drop_counts
                self._drop_counts = Counter()
                dropped = logging.LogRecord(
                    name="openkb.desktop_logging",
                    level=logging.WARNING,
                    pathname="",
                    lineno=0,
                    msg="diagnostic_events_dropped",
                    args=(),
                    exc_info=None,
                )
                dropped.openkb_event = "diagnostic_events_dropped"  # type: ignore[attr-defined]
                dropped.openkb_summary = "Diagnostic events were dropped under pressure."  # type: ignore[attr-defined]
                dropped.openkb_component = "runtime"  # type: ignore[attr-defined]
                dropped.openkb_fields = {  # type: ignore[attr-defined]
                    "dropped_trace": counts.get("trace", 0),
                    "dropped_debug": counts.get("debug", 0),
                    "dropped_info": counts.get("info", 0),
                    "queue_capacity": LOW_PRIORITY_QUEUE_CAPACITY,
                }
                self._write_one(dropped)
            self._write_one(record)

    def _write_one(self, record: logging.LogRecord) -> None:
        self._sequence += 1
        record.openkb_sequence = self._sequence  # type: ignore[attr-defined]
        try:
            self._target.emit(record)
        except Exception:
            return

    def flush(self) -> None:
        if hasattr(self, "_queue"):
            self._queue.join()
        if hasattr(self, "_target"):
            self._target.flush()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._queue.close()
        self._worker.join(timeout=5)
        self._target.flush()
        self._target.close()
        super().close()


def migrate_plaintext_logs(path: Path) -> tuple[Path, ...]:
    """Rename legacy plaintext files before opening the JSONL log set."""
    migrated: list[Path] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidates = (path,) + tuple(Path(f"{path}.{index}") for index in range(1, 10))
    for index, candidate in enumerate(candidates):
        if not candidate.is_file() or candidate.stat().st_size == 0:
            continue
        try:
            with candidate.open("r", encoding="utf-8") as stream:
                first_line = stream.readline()
            decoded = json.loads(first_line)
            structured = (
                isinstance(decoded, dict) and decoded.get("schema_version") == SCHEMA_VERSION
            )
        except (OSError, UnicodeError, ValueError):
            structured = False
        if structured:
            continue
        suffix = f"-part{index}" if index else ""
        destination = path.with_name(f"{path.stem}.legacy-{timestamp}{suffix}.log")
        counter = 1
        while destination.exists():
            destination = path.with_name(f"{path.stem}.legacy-{timestamp}{suffix}-{counter}.log")
            counter += 1
        candidate.replace(destination)
        migrated.append(destination)
    return tuple(migrated)


def sanitize_application_log_line(line: bytes, *, expected_process: str) -> bytes | None:
    """Rebuild one support-safe event before it may enter a Diagnostic Bundle."""
    if not line or len(line) > 256 * 1024:
        return None
    try:
        decoded = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict) or decoded.get("schema_version") != SCHEMA_VERSION:
        return None
    event = decoded.get("event")
    process = decoded.get("process")
    level = decoded.get("level")
    component = decoded.get("component")
    if (
        not isinstance(event, str)
        or _EVENT_PATTERN.fullmatch(event) is None
        or process != expected_process
        or level not in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR"}
        or component not in DIAGNOSTIC_COMPONENTS
    ):
        return None
    session = decoded.get("runtime_session_id")
    if not isinstance(session, str) or _EVENT_PATTERN.fullmatch(session.lower()) is None:
        session = "invalid-session"
    timestamp = decoded.get("timestamp")
    if not isinstance(timestamp, str) or len(timestamp) > 40:
        timestamp = "invalid-timestamp"
    rebuilt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp,
        "level": level,
        "event": event,
        "summary": event.replace("_", " ").capitalize() + ".",
        "runtime_session_id": session,
        "process": process,
        "pid": decoded.get("pid") if isinstance(decoded.get("pid"), int) else 0,
        "thread": "recorded",
        "component": component,
        "sequence": decoded.get("sequence") if isinstance(decoded.get("sequence"), int) else 0,
    }
    rebuilt.update(sanitize_event_fields(decoded))
    raw_stack = decoded.get("stack")
    if isinstance(raw_stack, list):
        stack: list[dict[str, object]] = []
        for frame in raw_stack[-20:]:
            if not isinstance(frame, dict):
                continue
            module = frame.get("module")
            function = frame.get("function")
            line_number = frame.get("line")
            if (
                isinstance(module, str)
                and isinstance(function, str)
                and isinstance(line_number, int)
            ):
                stack.append(
                    {
                        "module": module[:128],
                        "function": function[:128],
                        "line": line_number,
                    }
                )
        if stack:
            rebuilt["stack"] = stack
    return json.dumps(rebuilt, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
