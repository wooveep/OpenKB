"""Best-effort Knowledge Catalog lease boundary for retrieval."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from openkb.desktop_catalog_store import CatalogGenerationLease, lease_current_catalog

logger = logging.getLogger(__name__)


@contextmanager
def best_effort_catalog_lease(
    kb_dir: Path,
    *,
    enabled: bool,
    lease_factory: Callable[
        [Path], AbstractContextManager[CatalogGenerationLease | None]
    ] = lease_current_catalog,
) -> Iterator[tuple[CatalogGenerationLease | None, tuple[str, ...]]]:
    """Acquire an optional Catalog lease without making baseline retrieval depend on it."""
    if not enabled:
        yield None, ()
        return
    lease_manager = lease_factory(kb_dir)
    try:
        catalog = lease_manager.__enter__()
    except Exception:
        logger.warning("Knowledge Catalog lease failed; using baseline retrieval.", exc_info=True)
        yield None, ("catalog_query_failed",)
        return
    try:
        yield catalog, ()
    finally:
        try:
            lease_manager.__exit__(None, None, None)
        except Exception:
            logger.warning("Knowledge Catalog lease cleanup failed.", exc_info=True)
