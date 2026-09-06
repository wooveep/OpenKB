"""Low-fidelity private Tika adapter for legacy binary Office imports.

Binary DOC and PPT have a deliberately narrow Desktop contract: retain the
complete Raw Asset and extract best-effort text plus parser metadata.  They do
not pretend to have the page, slide, image, table, or layout fidelity of their
modern OOXML counterparts.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock, ParsedDocument

_TIKA_HOST = "127.0.0.1"
_TIKA_STARTUP_SECONDS = 20
_TIKA_REQUEST_SECONDS = 60
_TIKA_JAR_NAME = "tika-server-standard-3.3.2.jar"
_OLE_COMPOUND_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class _LegacyOfficeTikaRuntime:
    """Own one package-local Tika Server for the lifetime of the Python Engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._endpoint: str | None = None

    def endpoint(self, source: Path) -> str:
        """Start the loopback-only packaged server once and return its endpoint."""
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._endpoint:
                return self._endpoint
            self._stop_unlocked()
            java_path, jar_path = _runtime_paths()
            port = _available_port()
            try:
                self._process = subprocess.Popen(
                    [
                        str(java_path),
                        "-cp",
                        str(jar_path),
                        "org.apache.tika.server.core.TikaServerCli",
                        "--host",
                        _TIKA_HOST,
                        "--port",
                        str(port),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                raise _legacy_runtime_error(source) from error

            endpoint = f"http://{_TIKA_HOST}:{port}"
            try:
                _wait_for_server(self._process, port, source)
            except DesktopImportError:
                self._stop_unlocked()
                raise
            self._endpoint = endpoint
            return endpoint

    def shutdown(self) -> None:
        """Stop the private Java child before the Engine exits normally."""
        with self._lock:
            self._stop_unlocked()

    def _stop_unlocked(self) -> None:
        process, self._process = self._process, None
        self._endpoint = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


_TIKA_RUNTIME = _LegacyOfficeTikaRuntime()


def legacy_office_resources_available() -> bool:
    """Check packaged resource paths without starting Java or importing Tika."""
    try:
        _runtime_paths()
    except DesktopImportError:
        return False
    return importlib.util.find_spec("tika") is not None


def prewarm_legacy_office_runtime(source: Path | None = None) -> None:
    """Start the reusable private runtime for an imminent DOC/PPT parse."""
    _TIKA_RUNTIME.endpoint(source or Path("legacy-office.doc"))


def parse_legacy_office_document(
    source: Path, raw_bytes: bytes, source_format: str
) -> ParsedDocument:
    """Extract low-fidelity legacy Office text without exposing a public server."""
    if source_format not in {"doc", "ppt"}:
        raise DesktopImportError(
            "unsupported_import_format", f"No legacy Office parser is registered for {source.name}."
        )
    if not raw_bytes.startswith(_OLE_COMPOUND_MAGIC):
        raise _legacy_parse_error(source)
    try:
        extracted = _extract_with_tika(source, raw_bytes)
    except DesktopImportError:
        raise
    except Exception as error:
        raise _legacy_parse_error(source) from error

    content = extracted.get("content") if isinstance(extracted, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise _legacy_parse_error(source)

    text = _normalize_content(content)
    if not text:
        raise _legacy_parse_error(source)
    metadata = _metadata_projection(extracted.get("metadata"))
    block = DocumentIRBlock(
        block_id=uuid.uuid4().hex,
        ordinal=0,
        kind="paragraph",
        text=text,
        heading_path=(),
        line_start=1,
        line_end=max(1, text.count("\n") + 1),
        locator={
            "parser_route": "tika_legacy",
            "fidelity": "low",
            "source_format": source_format,
            "metadata": metadata,
        },
    )
    return ParsedDocument((block,), ())


def shutdown_legacy_office_runtime() -> None:
    """Release the package-local Java child during a clean Engine shutdown."""
    _TIKA_RUNTIME.shutdown()


def _extract_with_tika(source: Path, raw_bytes: bytes) -> Mapping[str, object]:
    """Send one in-memory Raw Asset to the already-running private Tika endpoint."""
    try:
        from tika import parser, tika
    except ImportError as error:
        raise _legacy_runtime_error(source) from error

    # The Desktop Runtime owns this endpoint.  Client-only mode prevents
    # python-tika from falling back to its default download-and-start behavior.
    tika.TikaClientOnly = True
    filename = source.name.replace('"', "_")
    try:
        result = parser.from_buffer(
            raw_bytes,
            serverEndpoint=_TIKA_RUNTIME.endpoint(source),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            requestOptions={"timeout": _TIKA_REQUEST_SECONDS},
        )
    except DesktopImportError:
        raise
    except Exception as error:
        raise _legacy_parse_error(source) from error
    if not isinstance(result, Mapping):
        raise _legacy_parse_error(source)
    return result


def _runtime_paths() -> tuple[Path, Path]:
    root = _runtime_root()
    java_name = "java.exe" if os.name == "nt" else "java"
    java_path = root / "java" / "bin" / java_name
    jar_path = root / "tika" / _TIKA_JAR_NAME
    if java_path.is_file() and jar_path.is_file():
        return java_path, jar_path
    raise DesktopImportError(
        "legacy_office_runtime_unavailable",
        "OpenKB's packaged legacy Office reader is incomplete. "
        "Convert the document to DOCX or PPTX and import it again.",
    )


def _runtime_root() -> Path:
    configured = os.environ.get("OPENKB_LEGACY_OFFICE_RUNTIME")
    if configured:
        return Path(configured)
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        return Path(bundle_root) / "legacy-office"
    raise DesktopImportError(
        "legacy_office_runtime_unavailable",
        "The packaged legacy Office reader is only available in OpenKB Desktop. "
        "Convert the document to DOCX or PPTX and import it again.",
    )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_handle:
        socket_handle.bind((_TIKA_HOST, 0))
        return int(socket_handle.getsockname()[1])


def _wait_for_server(process: subprocess.Popen[bytes], port: int, source: Path) -> None:
    deadline = time.monotonic() + _TIKA_STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with socket.create_connection((_TIKA_HOST, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise _legacy_runtime_error(source)


def _normalize_content(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def _metadata_projection(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, Mapping):
        return {}
    return {str(key): _json_value(value) for key, value in metadata.items()}


def _json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def _legacy_parse_error(source: Path) -> DesktopImportError:
    return DesktopImportError(
        "legacy_office_parse_failed",
        f"OpenKB could not extract usable text from {source.name}.",
        suggested_action=_legacy_conversion_advice(source),
    )


def _legacy_runtime_error(source: Path) -> DesktopImportError:
    return DesktopImportError(
        "legacy_office_runtime_unavailable",
        "OpenKB could not start its packaged legacy Office reader.",
        suggested_action=_legacy_conversion_advice(source),
    )


def _legacy_conversion_advice(source: Path) -> str:
    target_format = "DOCX" if source.suffix.casefold() == ".doc" else "PPTX"
    return f"Convert it to {target_format} and import it again."


atexit.register(shutdown_legacy_office_runtime)
