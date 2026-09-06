"""Concurrency invariants for operation-scoped model retry permits."""

from __future__ import annotations

import sqlite3

from openkb.models.operation_migrations import (
    MODEL_OPERATION_RETRY_MIGRATION_STATEMENTS,
    MODEL_OPERATION_RETRY_REVISION_MIGRATION_STATEMENTS,
    MODEL_OPERATION_STATE_MIGRATION_STATEMENTS,
)
from openkb.models.operation_state import DesktopModelOperationContractStore
from openkb.workspace.runtime import DesktopKnowledgeBaseRuntime, desktop_state_database_path


def test_retry_permit_is_bound_to_the_authorized_suspension_revision(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    contract = {
        "operation": "knowledge_graph_extraction",
        "capability_identity": "analysis-capability",
        "prompt_contract_digest": "graph-contract",
    }
    store.suspend(
        **contract,
        failure_code="knowledge_graph_response_invalid",
        reason="Original failure.",
        failure_stage="domain_validation",
    )
    assert store.authorize_retry(**contract, retry_scope="graph:document-1")
    assert store.dispatch_possible(**contract, retry_scope="graph:document-1")

    store.suspend(
        **contract,
        failure_code="knowledge_graph_response_invalid",
        reason="Newer concurrent failure.",
        failure_stage="domain_validation",
    )

    assert not store.dispatch_possible(**contract, retry_scope="graph:document-1")
    assert not store.authorize_retry(**contract, retry_scope="graph:document-1")
    assert not store.mark_ready_for_retry(**contract, retry_scope="graph:document-1")
    state = store.state(**contract)
    assert state.status == "suspended"
    assert state.reason == "Newer concurrent failure."

    store.revoke_retry_scope("graph:document-1")
    assert store.authorize_retry(**contract, retry_scope="graph:document-1")
    assert store.dispatch_possible(**contract, retry_scope="graph:document-1")


def test_current_schema_tracks_contract_and_retry_suspension_revisions(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)

    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        state_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(model_operation_contract_states)"
            ).fetchall()
        }
        permit_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(model_operation_retry_permits)"
            ).fetchall()
        }
        graph_task_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(knowledge_graph_extraction_tasks)"
            ).fetchall()
        }
        page_tree_task_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(document_page_tree_enrichment_tasks)"
            ).fetchall()
        }

    assert "revision" in state_columns
    assert "suspension_revision" in permit_columns
    assert "retry_scope" in graph_task_columns
    assert "retry_scope" in page_tree_task_columns


def test_open_discards_retry_permits_without_clearing_the_suspension(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    DesktopKnowledgeBaseRuntime().create(kb_dir)
    store = DesktopModelOperationContractStore(kb_dir)
    contract = {
        "operation": "knowledge_graph_extraction",
        "capability_identity": "analysis-capability",
        "prompt_contract_digest": "graph-contract",
    }
    store.suspend(
        **contract,
        failure_code="knowledge_graph_response_invalid",
        reason="Original failure.",
        failure_stage="domain_validation",
    )
    assert store.authorize_retry(**contract, retry_scope="graph:restart-bound-action")

    DesktopKnowledgeBaseRuntime().open(kb_dir)

    assert store.state(**contract).status == "suspended"
    assert not store.dispatch_possible(
        **contract,
        retry_scope="graph:restart-bound-action",
    )
    with sqlite3.connect(desktop_state_database_path(kb_dir)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM model_operation_retry_permits"
        ).fetchone() == (0,)


def test_revision_migration_invalidates_pre_revision_retry_permits() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute(MODEL_OPERATION_STATE_MIGRATION_STATEMENTS[0])
        connection.execute(MODEL_OPERATION_RETRY_MIGRATION_STATEMENTS[0])
        connection.execute(
            """
            INSERT INTO model_operation_contract_states (
                operation, capability_identity, prompt_contract_digest, status,
                created_at, updated_at
            ) VALUES ('knowledge_graph_extraction', 'analysis', 'graph', 'suspended',
                '2026-08-29T00:00:00+00:00', '2026-08-29T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO model_operation_retry_permits (
                operation, capability_identity, prompt_contract_digest, retry_scope, created_at
            ) VALUES ('knowledge_graph_extraction', 'analysis', 'graph', 'graph:document-1',
                '2026-08-29T00:00:00+00:00')
            """
        )

        for statement in MODEL_OPERATION_RETRY_REVISION_MIGRATION_STATEMENTS:
            connection.execute(statement)

        assert connection.execute(
            "SELECT revision FROM model_operation_contract_states"
        ).fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT suspension_revision FROM model_operation_retry_permits"
            ).fetchall()
            == []
        )
