"""Protocol method groups shared by Desktop Engine request lifecycle rules."""

CONTROL_METHODS = frozenset(
    {"engine.handshake", "engine.health", "engine.cancel", "engine.shutdown"}
)

WORKSPACE_METHODS = frozenset(
    {
        "workbench.create_knowledge_base",
        "workbench.open_knowledge_base",
        "workbench.active_knowledge_base",
        "workbench.inspect_import_sources",
        "workbench.import_text_document",
        "workbench.resume_import_job",
        "workbench.recover_import_job",
        "workbench.import_jobs",
        "workbench.read_raw_document",
        "workbench.ask_grounded",
        "workbench.retry_interrupted_answer",
        "workbench.grounded_answers",
        "workbench.conversations",
        "workbench.conversation",
        "workbench.create_conversation",
        "workbench.rename_conversation",
        "workbench.delete_conversation",
        "workbench.save_conversation_draft",
        "workbench.ask_conversation",
        "workbench.regenerate_conversation_answer",
        "workbench.select_answer_version",
        "workbench.global_search",
        "workbench.knowledge_pages",
        "workbench.knowledge_page",
        "workbench.save_knowledge_page",
        "workbench.publish_knowledge_page",
        "workbench.verify_knowledge_page",
        "workbench.search_knowledge_sources",
        "workbench.bind_knowledge_page_source",
        "workbench.document_version_candidates",
        "workbench.resolve_document_version_candidate",
        "workbench.knowledge_reconciliation_conflicts",
        "workbench.stage_knowledge_reconciliation_decisions",
        "workbench.commit_knowledge_reconciliation_decisions",
        "workbench.model_settings",
        "workbench.save_model_settings",
        "workbench.test_model_connection",
        "workbench.export_diagnostic_bundle",
    }
)

INTERRUPTION_PRESERVING_METHODS = frozenset(
    {
        "workbench.ask_grounded",
        "workbench.retry_interrupted_answer",
        "workbench.ask_conversation",
        "workbench.regenerate_conversation_answer",
    }
)

MODEL_SETTINGS_METHODS = frozenset(
    method for method in WORKSPACE_METHODS if "model_settings" in method
) | {"workbench.export_diagnostic_bundle", "workbench.test_model_connection"}

KNOWLEDGE_PAGE_METHODS = frozenset(
    method for method in WORKSPACE_METHODS if "knowledge_page" in method
) | {"workbench.search_knowledge_sources"}

NON_CANCELABLE_MUTATION_METHODS = frozenset(
    {
        "workbench.create_knowledge_base",
        "workbench.open_knowledge_base",
        "workbench.import_text_document",
        "workbench.read_raw_document",
        "workbench.save_knowledge_page",
        "workbench.publish_knowledge_page",
        "workbench.verify_knowledge_page",
        "workbench.bind_knowledge_page_source",
        "workbench.resolve_document_version_candidate",
        "workbench.stage_knowledge_reconciliation_decisions",
        "workbench.commit_knowledge_reconciliation_decisions",
        "workbench.save_model_settings",
        "workbench.create_conversation",
        "workbench.rename_conversation",
        "workbench.delete_conversation",
        "workbench.save_conversation_draft",
        "workbench.select_answer_version",
        "workbench.export_diagnostic_bundle",
    }
)
