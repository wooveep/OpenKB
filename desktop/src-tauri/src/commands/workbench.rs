//! Workbench commands mediating the private Engine protocol.

use super::run_engine;
use crate::diagnostics;
use crate::engine::protocol::{
    ActiveKnowledgeBaseResult, BridgeError, BridgeEvent, BridgeHandshake, CancelResult,
    DiagnosticBundleResult, EngineHealth, GroundedAnswer, GroundedAnswersResult,
    ImportControlResult, ImportJobsResult, ImportSourceInspection, KnowledgeBaseActivationResult,
    KnowledgeGraphExtractionControlResult, KnowledgeReconciliationCommit,
    KnowledgeReconciliationConflictsResult, KnowledgeReconciliationDecision,
    PageTreeEnrichmentControlResult, RawDocument, RecoveryOverride, TextDocumentImportResult,
    VersionFilter,
};
use crate::{runtime, DesktopState};
use std::path::Path;
use tauri::{ipc::Channel, State};

#[tauri::command]
pub(crate) async fn desktop_bridge_handshake(
    state: State<'_, DesktopState>,
) -> Result<BridgeHandshake, BridgeError> {
    run_engine(&state.engine, "handshake", move |engine| engine.handshake()).await
}

#[tauri::command]
pub(crate) async fn desktop_engine_health(
    _app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<EngineHealth, BridgeError> {
    run_engine(&state.engine, "health", |engine| engine.health())
        .await
        .inspect_err(|error| {
            diagnostics::logging::event(
                diagnostics::config::LogLevel::Warn,
                "bridge",
                "engine_health_check_failed",
                "Desktop Engine health check failed.",
                serde_json::json!({"error_code": error.code, "outcome": "failed"}),
            );
        })
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_create_knowledge_base(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
    kb_dir: String,
    name: Option<String>,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let activation = run_engine(&state.engine, "create_knowledge_base", move |engine| {
        engine.create_knowledge_base(kb_dir, name, request_id)
    })
    .await?;
    runtime::remember_active_knowledge_base(&app, &activation.knowledge_base.kb_dir);
    allow_source_images(&app, &activation)?;
    Ok(activation)
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_open_knowledge_base(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
    kb_dir: String,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let activation = run_engine(&state.engine, "open_knowledge_base", move |engine| {
        engine.open_knowledge_base(kb_dir, request_id)
    })
    .await?;
    runtime::remember_active_knowledge_base(&app, &activation.knowledge_base.kb_dir);
    allow_source_images(&app, &activation)?;
    Ok(activation)
}

#[tauri::command]
pub(crate) fn desktop_take_launch_intents(
    state: State<'_, DesktopState>,
) -> Vec<runtime::DesktopLaunchIntent> {
    state.runtime.take_launch_intents()
}

#[tauri::command]
pub(crate) async fn desktop_active_knowledge_base(
    state: State<'_, DesktopState>,
) -> Result<ActiveKnowledgeBaseResult, BridgeError> {
    run_engine(&state.engine, "active_knowledge_base", move |engine| {
        engine.active_knowledge_base()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) fn desktop_reveal_knowledge_base_directory(kb_dir: String) -> Result<(), BridgeError> {
    runtime::reveal_directory(Path::new(&kb_dir)).map_err(|message| BridgeError {
        code: "desktop_directory_open_failed".to_owned(),
        message,
    })
}

#[tauri::command]
pub(crate) fn desktop_quit_application(app: tauri::AppHandle) {
    app.exit(0);
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_open_external_url(url: String) -> Result<(), BridgeError> {
    tauri::async_runtime::spawn_blocking(move || {
        runtime::external_url::open_in_system_browser(&url)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop external URL open task stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_inspect_import_sources(
    state: State<'_, DesktopState>,
    source_paths: Vec<String>,
    request_id: String,
) -> Result<ImportSourceInspection, BridgeError> {
    run_engine(&state.engine, "inspect_import_sources", move |engine| {
        engine.inspect_import_sources(source_paths, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_import_text_document(
    state: State<'_, DesktopState>,
    source_path: String,
    parser_mode: Option<String>,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    run_engine(&state.engine, "import_text_document", move |engine| {
        engine.import_text_document(source_path, parser_mode, request_id)
    })
    .await
}

#[tauri::command]
pub(crate) async fn desktop_import_jobs(
    state: State<'_, DesktopState>,
) -> Result<ImportJobsResult, BridgeError> {
    run_engine(&state.engine, "import_jobs", move |engine| {
        engine.import_jobs()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_ask_grounded(
    state: State<'_, DesktopState>,
    question: String,
    version_filter: Option<VersionFilter>,
    request_id: String,
) -> Result<GroundedAnswer, BridgeError> {
    run_engine(&state.engine, "ask_grounded", move |engine| {
        engine.ask_grounded(question, version_filter, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_retry_interrupted_answer(
    state: State<'_, DesktopState>,
    answer_id: String,
    request_id: String,
) -> Result<GroundedAnswer, BridgeError> {
    run_engine(&state.engine, "retry_interrupted_answer", move |engine| {
        engine.retry_interrupted_answer(answer_id, request_id)
    })
    .await
}

#[tauri::command]
pub(crate) async fn desktop_grounded_answers(
    state: State<'_, DesktopState>,
) -> Result<GroundedAnswersResult, BridgeError> {
    run_engine(&state.engine, "grounded_answers", move |engine| {
        engine.grounded_answers()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_conversations(
    state: State<'_, DesktopState>,
    search: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "conversations", move |engine| {
        engine.conversations(search)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_global_search(
    state: State<'_, DesktopState>,
    query: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "global_search", move |engine| {
        engine.global_search(query)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "conversation", move |engine| {
        engine.conversation(conversation_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_create_conversation(
    state: State<'_, DesktopState>,
    title: Option<String>,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "create_conversation", move |engine| {
        engine.create_conversation(title, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_rename_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    title: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "rename_conversation", move |engine| {
        engine.rename_conversation(conversation_id, title, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_delete_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "delete_conversation", move |engine| {
        engine.delete_conversation(conversation_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_conversation_draft(
    state: State<'_, DesktopState>,
    conversation_id: String,
    draft_text: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "save_conversation_draft", move |engine| {
        engine.save_conversation_draft(conversation_id, draft_text, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_ask_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    question: String,
    version_filter: Option<VersionFilter>,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "ask_conversation", move |engine| {
        engine.ask_conversation(conversation_id, question, version_filter, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_regenerate_conversation_answer(
    state: State<'_, DesktopState>,
    conversation_id: String,
    assistant_message_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(
        &state.engine,
        "regenerate_conversation_answer",
        move |engine| {
            engine.regenerate_conversation_answer(conversation_id, assistant_message_id, request_id)
        },
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_select_answer_version(
    state: State<'_, DesktopState>,
    conversation_id: String,
    assistant_message_id: String,
    answer_version_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    run_engine(&state.engine, "select_answer_version", move |engine| {
        engine.select_answer_version(
            conversation_id,
            assistant_message_id,
            answer_version_id,
            request_id,
        )
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_export_diagnostic_bundle(
    state: State<'_, DesktopState>,
    destination: String,
    request_id: String,
) -> Result<DiagnosticBundleResult, BridgeError> {
    run_engine(&state.engine, "export_diagnostic_bundle", move |engine| {
        engine.export_diagnostic_bundle(destination, request_id)
    })
    .await
}

#[tauri::command]
pub(crate) async fn desktop_knowledge_reconciliation_conflicts(
    state: State<'_, DesktopState>,
) -> Result<KnowledgeReconciliationConflictsResult, BridgeError> {
    run_engine(
        &state.engine,
        "knowledge_reconciliation_conflicts",
        move |engine| engine.knowledge_reconciliation_conflicts(),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_stage_knowledge_reconciliation_decisions(
    state: State<'_, DesktopState>,
    candidate_ids: Vec<String>,
    decision: Option<KnowledgeReconciliationDecision>,
    manual_merge_content: Option<String>,
    request_id: String,
) -> Result<KnowledgeReconciliationConflictsResult, BridgeError> {
    run_engine(
        &state.engine,
        "stage_knowledge_reconciliation_decisions",
        move |engine| {
            engine.stage_knowledge_reconciliation_decisions(
                candidate_ids,
                decision,
                manual_merge_content,
                request_id,
            )
        },
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_commit_knowledge_reconciliation_decisions(
    state: State<'_, DesktopState>,
    request_id: String,
) -> Result<KnowledgeReconciliationCommit, BridgeError> {
    run_engine(
        &state.engine,
        "commit_knowledge_reconciliation_decisions",
        move |engine| engine.commit_knowledge_reconciliation_decisions(request_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_read_raw_document(
    state: State<'_, DesktopState>,
    document_id: String,
    request_id: String,
    page: u32,
    focus_locator: Option<serde_json::Value>,
) -> Result<RawDocument, BridgeError> {
    run_engine(&state.engine, "read_raw_document", move |engine| {
        engine.read_raw_document(document_id, request_id, page, focus_locator)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_pause_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
) -> Result<ImportControlResult, BridgeError> {
    run_engine(&state.engine, "pause_import_job", move |engine| {
        engine.pause_import_job(job_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_resume_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    run_engine(&state.engine, "resume_import_job", move |engine| {
        engine.resume_import_job(job_id, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_recover_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
    recovery_override: RecoveryOverride,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    run_engine(&state.engine, "recover_import_job", move |engine| {
        engine.recover_import_job(job_id, recovery_override, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_cancel_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
) -> Result<ImportControlResult, BridgeError> {
    run_engine(&state.engine, "cancel_import_job", move |engine| {
        engine.cancel_import_job(job_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_cancel_page_tree_enrichment(
    state: State<'_, DesktopState>,
    document_id: String,
) -> Result<PageTreeEnrichmentControlResult, BridgeError> {
    run_engine(
        &state.engine,
        "cancel_page_tree_enrichment",
        move |engine| engine.cancel_page_tree_enrichment(document_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_retry_page_tree_enrichment(
    state: State<'_, DesktopState>,
    document_id: String,
) -> Result<PageTreeEnrichmentControlResult, BridgeError> {
    run_engine(&state.engine, "retry_page_tree_enrichment", move |engine| {
        engine.retry_page_tree_enrichment(document_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_cancel_knowledge_graph_extraction(
    state: State<'_, DesktopState>,
    document_id: String,
) -> Result<KnowledgeGraphExtractionControlResult, BridgeError> {
    run_engine(
        &state.engine,
        "cancel_knowledge_graph_extraction",
        move |engine| engine.cancel_knowledge_graph_extraction(document_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_retry_knowledge_graph_extraction(
    state: State<'_, DesktopState>,
    document_id: String,
) -> Result<KnowledgeGraphExtractionControlResult, BridgeError> {
    run_engine(
        &state.engine,
        "retry_knowledge_graph_extraction",
        move |engine| engine.retry_knowledge_graph_extraction(document_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_cancel(
    state: State<'_, DesktopState>,
    target_request_id: String,
) -> Result<CancelResult, BridgeError> {
    run_engine(&state.engine, "cancel", move |engine| {
        engine.cancel(target_request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_subscribe(
    state: State<'_, DesktopState>,
    subscription_id: String,
    event_channel: Channel<BridgeEvent>,
) -> Result<(), BridgeError> {
    run_engine(&state.engine, "subscribe", move |engine| {
        engine.subscribe(subscription_id, event_channel)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) fn desktop_unsubscribe(state: State<'_, DesktopState>, subscription_id: String) {
    state.engine.unsubscribe(&subscription_id);
}

fn allow_source_images(
    app: &tauri::AppHandle,
    activation: &KnowledgeBaseActivationResult,
) -> Result<(), BridgeError> {
    runtime::allow_source_image_directory(app, &activation.knowledge_base.kb_dir).map_err(
        |message| BridgeError {
            code: "desktop_source_image_scope_failed".to_owned(),
            message,
        },
    )
}
