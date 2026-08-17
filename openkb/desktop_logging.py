"""Small rotating application log setup for the packaged Desktop Engine."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ENGINE_LOG_FILE = "openkb-engine.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def desktop_application_log_directory() -> Path:
    """Return the auto-created application log location, outside every knowledge base."""
    configured = os.environ.get("OPENKB_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "OpenKB" / "logs"
    return Path.home() / ".local" / "share" / "OpenKB" / "logs"


def configure_desktop_engine_logging() -> Path | None:
    """Route OpenKB Engine diagnostics to a small rotating file set."""
    directory = desktop_application_log_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _ENGINE_LOG_FILE
        logger = logging.getLogger("openkb")
        if any(
            getattr(handler, "_openkb_desktop_engine_log", False) for handler in logger.handlers
        ):
            return path
        handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler._openkb_desktop_engine_log = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return path
    except OSError:
        return None
