"""Explicitly authorized, failure-scoped raw diagnostic evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from openkb.desktop_logging_settings import DIAGNOSTIC_COMPONENTS, DiagnosticLoggingSettings

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
PAYLOAD_EDGE_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 100 * 1024 * 1024
MAX_CAPTURE_DIRECTORIES = 2
MAX_RETENTION = timedelta(hours=24)
MAX_EVENT_METADATA_BYTES = 64 * 1024
MAX_METADATA_DEPTH = 8
MAX_COLLECTION_ITEMS = 128
_CAPTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ACTIVE_CAPTURE: SensitiveTraceCapture | None = None
_ACTIVE_CAPTURE_LOCK = threading.Lock()
_ACTIVE_MONITOR_STOP: threading.Event | None = None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _integer(value: object) -> int:
    return value if isinstance(value, int) else 0


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for candidate in path.rglob("*"):
            if candidate.is_file():
                total += candidate.stat().st_size
    except OSError:
        return total
    return total


def _read_manifest(directory: Path) -> dict[str, object]:
    try:
        decoded = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _capture_sort_key(directory: Path) -> tuple[str, str]:
    manifest = _read_manifest(directory)
    started_at = manifest.get("started_at")
    return (started_at if isinstance(started_at, str) else "", directory.name)


def _prune_capture_root(
    root: Path,
    *,
    keep_id: str | None,
    now: datetime,
    reserve_new_slot: bool,
) -> None:
    directories = [candidate for candidate in root.iterdir() if candidate.is_dir()]
    for directory in directories:
        if keep_id is not None and directory.name == keep_id:
            continue
        manifest = _read_manifest(directory)
        expires_at = _parse_time(manifest.get("expires_at"))
        started_at = _parse_time(manifest.get("started_at"))
        try:
            modified_at = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
        except OSError:
            modified_at = now
        if (
            (expires_at is not None and expires_at <= now)
            or (started_at is not None and started_at + MAX_RETENTION <= now)
            or (expires_at is None and started_at is None and modified_at + MAX_RETENTION <= now)
        ):
            shutil.rmtree(directory, ignore_errors=True)

    directories = sorted(
        (candidate for candidate in root.iterdir() if candidate.is_dir()),
        key=_capture_sort_key,
    )
    keep_exists = any(directory.name == keep_id for directory in directories)
    maximum_existing = MAX_CAPTURE_DIRECTORIES - int(reserve_new_slot and not keep_exists)
    while len(directories) > maximum_existing:
        victim = next(
            directory for directory in directories if keep_id is None or directory.name != keep_id
        )
        shutil.rmtree(victim, ignore_errors=True)
        directories.remove(victim)

    while sum(_directory_size(directory) for directory in directories) > MAX_CAPTURE_BYTES:
        victims = [directory for directory in directories if directory.name != keep_id]
        if not victims:
            break
        victim = victims[0]
        shutil.rmtree(victim, ignore_errors=True)
        directories.remove(victim)


def _bounded_text(value: str, maximum_bytes: int = MAX_EVENT_METADATA_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="replace")


def _json_compatible(value: object, *, depth: int = 0) -> object:
    if depth >= MAX_METADATA_DEPTH:
        return "<maximum-depth-reached>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return _bounded_text(value) if isinstance(value, str) else value
    if isinstance(value, bytes):
        return _bounded_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, Path):
        return _bounded_text(str(value))
    if isinstance(value, Mapping):
        return {
            _bounded_text(str(key), 256): _json_compatible(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _json_compatible(item, depth=depth + 1) for item in list(value)[:MAX_COLLECTION_ITEMS]
        ]
    return _bounded_text(repr(value))


def _bounded_metadata(value: Mapping[str, object]) -> tuple[object, bool]:
    compatible = _json_compatible(value)
    encoded = json.dumps(compatible, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_EVENT_METADATA_BYTES:
        return compatible, False
    edge = MAX_EVENT_METADATA_BYTES // 2
    return (
        {
            "truncated": True,
            "full_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "head": encoded[:edge].decode("utf-8", errors="replace"),
            "tail": encoded[-edge:].decode("utf-8", errors="replace"),
        },
        True,
    )


@dataclass(frozen=True)
class SensitiveTracePayload:
    label: str
    relative_path: str
    sha256: str
    full_bytes: int
    stored_bytes: int
    truncated: bool


@dataclass(frozen=True)
class SensitiveTraceRecord:
    event_id: str
    payloads: tuple[SensitiveTracePayload, ...]


class SensitiveTraceCapture:
    """One bounded capture directory; callers add evidence only on failure."""

    def __init__(
        self,
        settings: DiagnosticLoggingSettings,
        directory: Path,
        manifest: dict[str, object],
    ) -> None:
        self.settings = settings
        self.directory = directory
        self._manifest = manifest
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        settings: DiagnosticLoggingSettings,
        *,
        app_version: str,
        build: str,
        now: datetime | None = None,
    ) -> SensitiveTraceCapture | None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        capture_id = settings.sensitive_trace_capture_id
        root = settings.sensitive_trace_root
        if not settings.trace_components or not settings.sensitive_trace_enabled_at(current):
            return None
        if root is None or capture_id is None or not _CAPTURE_ID_PATTERN.fullmatch(capture_id):
            return None
        try:
            root.mkdir(parents=True, exist_ok=True)
            _chmod(root, 0o700)
            _prune_capture_root(
                root,
                keep_id=capture_id,
                now=current,
                reserve_new_slot=True,
            )
            directory = root / capture_id
            payload_directory = directory / "payloads"
            payload_directory.mkdir(parents=True, exist_ok=True)
            _chmod(directory, 0o700)
            _chmod(payload_directory, 0o700)
        except OSError:
            return None

        existing = _read_manifest(directory)
        effective_levels = {
            component: settings.effective_level_name(component, now=current)
            for component in DIAGNOSTIC_COMPONENTS
        }
        manifest: dict[str, object]
        if existing.get("capture_id") == capture_id:
            manifest = existing
        else:
            expires_at = settings.sensitive_trace_expires_at
            if expires_at is None:
                return None
            manifest = {
                "schema_version": 1,
                "capture_id": capture_id,
                "runtime_session_id": settings.runtime_session_id,
                "app_version": app_version,
                "build": build,
                "started_at": _utc_text(current),
                "ended_at": None,
                "expires_at": _utc_text(expires_at),
                "stop_reason": None,
                "configured_level": settings.level_name,
                "component_levels": dict(settings.component_levels),
                "trace_components": list(settings.trace_components),
                "effective_levels": effective_levels,
                "complete": False,
                "incomplete_reasons": ["capture_active"],
                "event_count": 0,
                "payload_count": 0,
                "dropped_event_count": 0,
                "dropped_payload_count": 0,
                "truncated_metadata_count": 0,
                "truncated_payload_count": 0,
                "bytes_stored": 0,
                "payloads": [],
            }
        manifest.setdefault("effective_levels", effective_levels)
        manifest.setdefault("complete", False)
        manifest.setdefault("incomplete_reasons", ["capture_active"])
        capture = cls(settings, directory, manifest)
        try:
            capture._write_manifest()
        except OSError:
            return None
        return capture

    def _is_active(self, now: datetime) -> bool:
        if self._manifest.get("ended_at") is not None:
            return False
        if self.settings.sensitive_trace_enabled_at(now):
            return True
        reason = "authorization_expired"
        stop_file = self.settings.sensitive_trace_stop_file
        if stop_file is not None and stop_file.exists():
            reason = "user_requested"
        self._finish(reason, now)
        return False

    def record_failure(
        self,
        event: str,
        *,
        metadata: Mapping[str, object] | None = None,
        payloads: Mapping[str, str | bytes] | None = None,
        now: datetime | None = None,
    ) -> SensitiveTraceRecord | None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            if not self._is_active(current):
                return None
            references: list[SensitiveTracePayload] = []
            for label, value in (payloads or {}).items():
                reference = self._store_payload(str(label), value)
                if reference is not None:
                    references.append(reference)
            event_id = str(uuid.uuid4())
            bounded_metadata, metadata_truncated = _bounded_metadata(metadata or {})
            event_record = {
                "schema_version": 1,
                "timestamp": _utc_text(current),
                "event_id": event_id,
                "event": str(event)[:128],
                "metadata": bounded_metadata,
                "payloads": [asdict(reference) for reference in references],
            }
            if metadata_truncated:
                self._manifest["truncated_metadata_count"] = (
                    _integer(self._manifest.get("truncated_metadata_count")) + 1
                )
            if not self._append_event(event_record):
                self._manifest["dropped_event_count"] = (
                    _integer(self._manifest.get("dropped_event_count")) + 1
                )
                self._write_manifest()
                return None
            self._manifest["event_count"] = _integer(self._manifest.get("event_count")) + 1
            self._write_manifest()
            return SensitiveTraceRecord(event_id=event_id, payloads=tuple(references))

    def _store_payload(self, label: str, value: str | bytes) -> SensitiveTracePayload | None:
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        digest = hashlib.sha256(raw).hexdigest()
        truncated = len(raw) > MAX_PAYLOAD_BYTES
        stored = raw[:PAYLOAD_EDGE_BYTES] + raw[-PAYLOAD_EDGE_BYTES:] if truncated else raw
        relative_path = f"payloads/{digest}.payload"
        path = self.directory / relative_path
        existing_payloads = self._manifest.get("payloads", [])
        if not isinstance(existing_payloads, list):
            existing_payloads = []
            self._manifest["payloads"] = existing_payloads
        existing = next(
            (
                item
                for item in existing_payloads
                if isinstance(item, dict) and item.get("sha256") == digest
            ),
            None,
        )
        if existing is not None:
            return SensitiveTracePayload(
                label=label[:128],
                relative_path=str(existing["relative_path"]),
                sha256=digest,
                full_bytes=int(existing["full_bytes"]),
                stored_bytes=int(existing["stored_bytes"]),
                truncated=bool(existing["truncated"]),
            )
        root = self.settings.sensitive_trace_root
        total = _directory_size(root) if root is not None and root.exists() else 0
        if total + len(stored) > MAX_CAPTURE_BYTES:
            self._manifest["dropped_payload_count"] = (
                _integer(self._manifest.get("dropped_payload_count")) + 1
            )
            return None
        try:
            path.write_bytes(stored)
            _chmod(path, 0o600)
        except OSError:
            self._manifest["dropped_payload_count"] = (
                _integer(self._manifest.get("dropped_payload_count")) + 1
            )
            return None
        reference = SensitiveTracePayload(
            label=label[:128],
            relative_path=relative_path,
            sha256=digest,
            full_bytes=len(raw),
            stored_bytes=len(stored),
            truncated=truncated,
        )
        existing_payloads.append(asdict(reference))
        self._manifest["payload_count"] = _integer(self._manifest.get("payload_count")) + 1
        self._manifest["bytes_stored"] = _integer(self._manifest.get("bytes_stored")) + len(stored)
        if truncated:
            self._manifest["truncated_payload_count"] = (
                _integer(self._manifest.get("truncated_payload_count")) + 1
            )
        return reference

    def _append_event(self, event: Mapping[str, object]) -> bool:
        path = self.directory / "events.jsonl"
        encoded = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        root = self.settings.sensitive_trace_root
        total = _directory_size(root) if root is not None and root.exists() else 0
        if total + len(encoded) > MAX_CAPTURE_BYTES:
            return False
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded.decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _chmod(path, 0o600)
        return True

    def _write_manifest(self) -> None:
        path = self.directory / "capture.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _chmod(temporary, 0o600)
        temporary.replace(path)
        _chmod(path, 0o600)

    def _finish(self, reason: str, now: datetime) -> None:
        if self._manifest.get("ended_at") is not None:
            return
        self._manifest["ended_at"] = _utc_text(now)
        self._manifest["stop_reason"] = reason
        incomplete_reasons: list[str] = []
        if _integer(self._manifest.get("dropped_event_count")):
            incomplete_reasons.append("events_dropped")
        if _integer(self._manifest.get("dropped_payload_count")):
            incomplete_reasons.append("payloads_dropped")
        self._manifest["complete"] = not incomplete_reasons
        self._manifest["incomplete_reasons"] = incomplete_reasons
        self._write_manifest()

    def stop(self, reason: str = "user_requested", *, now: datetime | None = None) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            stop_file = self.settings.sensitive_trace_stop_file
            if stop_file is not None:
                try:
                    stop_file.parent.mkdir(parents=True, exist_ok=True)
                    stop_file.touch(exist_ok=True)
                    _chmod(stop_file, 0o600)
                except OSError:
                    pass
            self._finish(reason, current)


def configure_sensitive_trace(
    settings: DiagnosticLoggingSettings,
    *,
    app_version: str,
    build: str,
) -> SensitiveTraceCapture | None:
    """Open the process singleton after ordinary Application Logs are ready."""
    global _ACTIVE_CAPTURE, _ACTIVE_MONITOR_STOP
    with _ACTIVE_CAPTURE_LOCK:
        if _ACTIVE_CAPTURE is None:
            root = settings.sensitive_trace_root
            if root is not None and root.is_dir() and not settings.trace_components:
                try:
                    _prune_capture_root(
                        root,
                        keep_id=None,
                        now=datetime.now(timezone.utc),
                        reserve_new_slot=False,
                    )
                except OSError:
                    pass
            _ACTIVE_CAPTURE = SensitiveTraceCapture.open(
                settings,
                app_version=app_version,
                build=build,
            )
            if _ACTIVE_CAPTURE is not None:
                _ACTIVE_MONITOR_STOP = threading.Event()
                monitor = threading.Thread(
                    target=_monitor_active_capture,
                    args=(_ACTIVE_CAPTURE, _ACTIVE_MONITOR_STOP),
                    name="openkb-sensitive-trace-monitor",
                    daemon=True,
                )
                monitor.start()
        return _ACTIVE_CAPTURE


def _monitor_active_capture(
    capture: SensitiveTraceCapture,
    stop_event: threading.Event,
) -> None:
    expires_at = capture.settings.sensitive_trace_expires_at
    stop_recorded = False
    while not stop_event.wait(1.0):
        stop_file = capture.settings.sensitive_trace_stop_file
        if not stop_recorded and stop_file is not None and stop_file.exists():
            with capture._lock:
                try:
                    capture._finish("user_requested", datetime.now(timezone.utc))
                except OSError:
                    pass
            stop_recorded = True
        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            _close_active_capture(capture, "authorization_expired", delete=True)
            return


def _close_active_capture(
    capture: SensitiveTraceCapture,
    reason: str,
    *,
    delete: bool,
) -> None:
    global _ACTIVE_CAPTURE, _ACTIVE_MONITOR_STOP
    with _ACTIVE_CAPTURE_LOCK:
        if _ACTIVE_CAPTURE is not capture:
            return
        if _ACTIVE_MONITOR_STOP is not None:
            _ACTIVE_MONITOR_STOP.set()
        with capture._lock:
            try:
                capture._finish(reason, datetime.now(timezone.utc))
            except OSError:
                pass
            if delete:
                shutil.rmtree(capture.directory, ignore_errors=True)
        _ACTIVE_CAPTURE = None
        _ACTIVE_MONITOR_STOP = None


def _expire_active_capture(capture: SensitiveTraceCapture) -> None:
    _close_active_capture(capture, "authorization_expired", delete=True)


def record_sensitive_trace_failure(
    event: str,
    *,
    metadata: Mapping[str, object] | None = None,
    payloads: Mapping[str, str | bytes] | None = None,
) -> SensitiveTraceRecord | None:
    """Best-effort raw evidence capture; never changes the failing operation."""
    capture = _ACTIVE_CAPTURE
    if capture is None:
        return None
    try:
        return capture.record_failure(event, metadata=metadata, payloads=payloads)
    except Exception:
        return None


def sensitive_trace_is_active() -> bool:
    capture = _ACTIVE_CAPTURE
    return capture is not None and capture.settings.sensitive_trace_enabled


def sensitive_trace_component_enabled(component: str) -> bool:
    capture = _ACTIVE_CAPTURE
    try:
        return (
            capture is not None
            and capture.settings.sensitive_trace_enabled
            and component in capture.settings.trace_components
        )
    except Exception:
        return False


def stop_active_sensitive_trace(reason: str = "user_requested") -> None:
    capture = _ACTIVE_CAPTURE
    if capture is not None:
        capture.stop(reason)


def close_active_sensitive_trace(reason: str = "runtime_stopped") -> None:
    """Finish the active manifest without creating a user-requested stop marker."""
    capture = _ACTIVE_CAPTURE
    if capture is not None:
        _close_active_capture(capture, reason, delete=False)


def reset_sensitive_trace_for_tests() -> None:
    global _ACTIVE_CAPTURE, _ACTIVE_MONITOR_STOP
    with _ACTIVE_CAPTURE_LOCK:
        if _ACTIVE_MONITOR_STOP is not None:
            _ACTIVE_MONITOR_STOP.set()
            _ACTIVE_MONITOR_STOP = None
        _ACTIVE_CAPTURE = None
