"""Atomic user-action and recovery boundaries for PageTree enrichment tasks."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_result_failure import (
    authorize_model_operation_retry_group_in,
    revoke_model_operation_retry_scope_in,
)
from openkb.desktop_page_tree_enrichment_control import (
    recover_interrupted_in,
    request_cancel_in,
    retry_document_in,
)
from openkb.desktop_structured_output import structured_output_repair_contract_digest
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock


class DesktopPageTreeEnrichmentActions:
    """Publish task state and its model authority through one SQLite commit."""

    def __init__(self, kb_dir: Path, *, prompt_digest: str) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)
        self.prompt_digest = prompt_digest

    def recover_interrupted(self) -> int:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    recovered, retry_scopes = recover_interrupted_in(connection)
                    for retry_scope in retry_scopes:
                        revoke_model_operation_retry_scope_in(connection, retry_scope)
                    return recovered
            finally:
                connection.close()

    def request_cancel(self, document_id: str) -> bool:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    accepted, retry_scope = request_cancel_in(connection, document_id)
                    if accepted and retry_scope is not None:
                        revoke_model_operation_retry_scope_in(connection, retry_scope)
                    return accepted
            finally:
                connection.close()

    def retry_document(self, document_id: str, gateway: DesktopModelGateway) -> bool:
        retry_scope = _new_retry_scope(document_id)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    accepted, previous_scope = retry_document_in(
                        connection,
                        document_id,
                        provider=gateway.provider_name,
                        model=gateway.model_name,
                        prompt_digest=self.prompt_digest,
                        retry_scope=retry_scope,
                    )
                    if not accepted:
                        return False
                    authorize_model_operation_retry_group_in(
                        connection,
                        gateway,
                        retry_scope=retry_scope,
                        contracts=(
                            ("page_tree_enrichment", self.prompt_digest),
                            (
                                "structured_output_repair",
                                structured_output_repair_contract_digest(
                                    "page_tree_enrichment"
                                ),
                            ),
                        ),
                    )
                    if previous_scope is not None:
                        revoke_model_operation_retry_scope_in(connection, previous_scope)
                    return True
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _new_retry_scope(document_id: str) -> str:
    return f"page_tree_enrichment:{document_id}:{uuid.uuid4().hex}"
