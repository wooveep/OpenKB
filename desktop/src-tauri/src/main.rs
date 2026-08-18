#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

//! OpenKB Desktop Shell: native window ownership and typed Engine mediation.

mod desktop_runtime;
mod engine_protocol;
mod engine_wire;
mod external_url;
mod process_tree;

use desktop_runtime::DesktopRuntimeState;
use engine_protocol::{
    ActiveKnowledgeBaseResult, BridgeError, BridgeEvent, BridgeHandshake, CancelResult,
    DiagnosticBundleResult, DocumentVersionCandidate, DocumentVersionCandidateDecision,
    DocumentVersionCandidatesResult, EngineHealth, EngineSupervisor, GroundedAnswer,
    GroundedAnswersResult, ImportControlResult, ImportJobsResult, ImportSourceInspection,
    KnowledgeBaseActivationResult, KnowledgePage, KnowledgePageKind, KnowledgePagesResult,
    KnowledgeReconciliationCommit, KnowledgeReconciliationConflictsResult,
    KnowledgeReconciliationDecision, ModelSettings, RawDocument, RecoveryOverride,
    TextDocumentImportResult,
};
use process_tree::ProcessTreeJob;
use std::{path::Path, sync::Arc};
use tauri::{ipc::Channel, Manager, State};

pub(crate) struct DesktopState {
    pub(crate) engine: Arc<EngineSupervisor>,
    _process_tree: ProcessTreeJob,
    pub(crate) runtime: DesktopRuntimeState,
}

macro_rules! desktop_join_error {
    ($operation:literal) => {
        |error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop {} task stopped unexpectedly: {error}", $operation),
        }
    };
}

#[tauri::command]
fn desktop_bridge_handshake(
    state: State<'_, DesktopState>,
) -> Result<BridgeHandshake, BridgeError> {
    state.engine.handshake()
}

#[tauri::command]
fn desktop_engine_health(state: State<'_, DesktopState>) -> Result<EngineHealth, BridgeError> {
    state.engine.health()
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_create_knowledge_base(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
    kb_dir: String,
    name: Option<String>,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    let activation = tauri::async_runtime::spawn_blocking(move || {
        engine.create_knowledge_base(kb_dir, name, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-base creation task stopped unexpectedly: {error}"),
    })??;
    desktop_runtime::remember_active_knowledge_base(&app, &activation.knowledge_base.kb_dir);
    allow_source_images(&app, &activation)?;
    Ok(activation)
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_open_knowledge_base(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
    kb_dir: String,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    let activation = tauri::async_runtime::spawn_blocking(move || {
        engine.open_knowledge_base(kb_dir, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-base open task stopped unexpectedly: {error}"),
    })??;
    desktop_runtime::remember_active_knowledge_base(&app, &activation.knowledge_base.kb_dir);
    allow_source_images(&app, &activation)?;
    Ok(activation)
}

#[tauri::command]
fn desktop_take_launch_intents(
    state: State<'_, DesktopState>,
) -> Vec<desktop_runtime::DesktopLaunchIntent> {
    state.runtime.take_launch_intents()
}

#[tauri::command]
async fn desktop_active_knowledge_base(
    state: State<'_, DesktopState>,
) -> Result<ActiveKnowledgeBaseResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.active_knowledge_base())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop active knowledge-base task stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_reveal_knowledge_base_directory(kb_dir: String) -> Result<(), BridgeError> {
    desktop_runtime::reveal_directory(Path::new(&kb_dir)).map_err(|message| BridgeError {
        code: "desktop_directory_open_failed".to_owned(),
        message,
    })
}

#[tauri::command]
fn desktop_reveal_application_log_directory(app: tauri::AppHandle) -> Result<(), BridgeError> {
    desktop_runtime::reveal_application_log_directory(&app).map_err(|message| BridgeError {
        code: "desktop_directory_open_failed".to_owned(),
        message,
    })
}

#[tauri::command]
fn desktop_quit_application(app: tauri::AppHandle) {
    app.exit(0);
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_open_external_url(url: String) -> Result<(), BridgeError> {
    tauri::async_runtime::spawn_blocking(move || external_url::open_in_system_browser(&url))
        .await
        .map_err(desktop_join_error!("external URL open"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_inspect_import_sources(
    state: State<'_, DesktopState>,
    source_paths: Vec<String>,
    request_id: String,
) -> Result<ImportSourceInspection, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.inspect_import_sources(source_paths, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop import source inspection stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_import_text_document(
    state: State<'_, DesktopState>,
    source_path: String,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.import_text_document(source_path, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop document import task stopped unexpectedly: {error}"),
    })?
}

#[tauri::command]
async fn desktop_import_jobs(
    state: State<'_, DesktopState>,
) -> Result<ImportJobsResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.import_jobs())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop import task lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_ask_grounded(
    state: State<'_, DesktopState>,
    question: String,
    request_id: String,
) -> Result<GroundedAnswer, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.ask_grounded(question, request_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop grounded answer task stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_retry_interrupted_answer(
    state: State<'_, DesktopState>,
    answer_id: String,
    request_id: String,
) -> Result<GroundedAnswer, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.retry_interrupted_answer(answer_id, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop interrupted-answer retry task stopped unexpectedly: {error}"),
    })?
}

#[tauri::command]
async fn desktop_grounded_answers(
    state: State<'_, DesktopState>,
) -> Result<GroundedAnswersResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.grounded_answers())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop grounded answer history stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_conversations(
    state: State<'_, DesktopState>,
    search: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.conversations(search))
        .await
        .map_err(desktop_join_error!("conversation list"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_global_search(
    state: State<'_, DesktopState>,
    query: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.global_search(query))
        .await
        .map_err(desktop_join_error!("global search"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.conversation(conversation_id))
        .await
        .map_err(desktop_join_error!("conversation read"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_create_conversation(
    state: State<'_, DesktopState>,
    title: Option<String>,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.create_conversation(title, request_id))
        .await
        .map_err(desktop_join_error!("conversation creation"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_rename_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    title: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.rename_conversation(conversation_id, title, request_id)
    })
    .await
    .map_err(desktop_join_error!("conversation rename"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_delete_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.delete_conversation(conversation_id, request_id)
    })
    .await
    .map_err(desktop_join_error!("conversation deletion"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_save_conversation_draft(
    state: State<'_, DesktopState>,
    conversation_id: String,
    draft_text: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.save_conversation_draft(conversation_id, draft_text, request_id)
    })
    .await
    .map_err(desktop_join_error!("conversation draft save"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_ask_conversation(
    state: State<'_, DesktopState>,
    conversation_id: String,
    question: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.ask_conversation(conversation_id, question, request_id)
    })
    .await
    .map_err(desktop_join_error!("conversation answer"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_regenerate_conversation_answer(
    state: State<'_, DesktopState>,
    conversation_id: String,
    assistant_message_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.regenerate_conversation_answer(conversation_id, assistant_message_id, request_id)
    })
    .await
    .map_err(desktop_join_error!("conversation answer regeneration"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_select_answer_version(
    state: State<'_, DesktopState>,
    conversation_id: String,
    assistant_message_id: String,
    answer_version_id: String,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.select_answer_version(
            conversation_id,
            assistant_message_id,
            answer_version_id,
            request_id,
        )
    })
    .await
    .map_err(desktop_join_error!("answer version selection"))?
}

#[tauri::command]
async fn desktop_model_settings(
    state: State<'_, DesktopState>,
) -> Result<ModelSettings, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.model_settings())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop model-settings lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_save_model_settings(
    state: State<'_, DesktopState>,
    provider: String,
    model: String,
    api_base_url: String,
    api_key: String,
    max_concurrent_model_calls: u32,
    initial_timeout_seconds: f64,
    request_id: String,
) -> Result<ModelSettings, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.save_model_settings(
            provider,
            model,
            api_base_url,
            api_key,
            max_concurrent_model_calls,
            initial_timeout_seconds,
            request_id,
        )
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop model-settings save stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
#[allow(clippy::too_many_arguments)]
async fn desktop_test_model_connection(
    state: State<'_, DesktopState>,
    provider: String,
    model: String,
    api_base_url: String,
    api_key: String,
    max_concurrent_model_calls: u32,
    initial_timeout_seconds: f64,
    request_id: String,
) -> Result<serde_json::Value, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.test_model_connection(
            provider,
            model,
            api_base_url,
            api_key,
            max_concurrent_model_calls,
            initial_timeout_seconds,
            request_id,
        )
    })
    .await
    .map_err(desktop_join_error!("model connection test"))?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_export_diagnostic_bundle(
    state: State<'_, DesktopState>,
    destination: String,
    request_id: String,
) -> Result<DiagnosticBundleResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.export_diagnostic_bundle(destination, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop diagnostic-bundle export stopped unexpectedly: {error}"),
    })?
}

#[tauri::command]
async fn desktop_knowledge_pages(
    state: State<'_, DesktopState>,
) -> Result<KnowledgePagesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_pages())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop knowledge-page lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_get_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_page(page_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop knowledge-page read stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_save_knowledge_page(
    state: State<'_, DesktopState>,
    page_id: Option<String>,
    kind: KnowledgePageKind,
    title: String,
    content_markdown: String,
    request_id: String,
) -> Result<KnowledgePage, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.save_knowledge_page(page_id, kind, title, content_markdown, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-page save stopped unexpectedly: {error}"),
    })?
}

#[tauri::command]
async fn desktop_document_version_candidates(
    state: State<'_, DesktopState>,
) -> Result<DocumentVersionCandidatesResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.document_version_candidates())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop document-version lookup stopped unexpectedly: {error}"),
        })?
}

#[tauri::command]
async fn desktop_knowledge_reconciliation_conflicts(
    state: State<'_, DesktopState>,
) -> Result<KnowledgeReconciliationConflictsResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.knowledge_reconciliation_conflicts())
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!(
                "Desktop knowledge-reconciliation lookup stopped unexpectedly: {error}"
            ),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_stage_knowledge_reconciliation_decisions(
    state: State<'_, DesktopState>,
    candidate_ids: Vec<String>,
    decision: Option<KnowledgeReconciliationDecision>,
    request_id: String,
) -> Result<KnowledgeReconciliationConflictsResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.stage_knowledge_reconciliation_decisions(candidate_ids, decision, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-reconciliation staging stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_commit_knowledge_reconciliation_decisions(
    state: State<'_, DesktopState>,
    request_id: String,
) -> Result<KnowledgeReconciliationCommit, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.commit_knowledge_reconciliation_decisions(request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-reconciliation commit stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_resolve_document_version_candidate(
    state: State<'_, DesktopState>,
    candidate_id: String,
    decision: DocumentVersionCandidateDecision,
    request_id: String,
) -> Result<DocumentVersionCandidate, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.resolve_document_version_candidate(candidate_id, decision, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop document-version decision stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_read_raw_document(
    state: State<'_, DesktopState>,
    document_id: String,
    request_id: String,
    page: u32,
    focus_locator: Option<serde_json::Value>,
) -> Result<RawDocument, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.read_raw_document(document_id, request_id, page, focus_locator)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop original-document read stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_pause_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
) -> Result<ImportControlResult, BridgeError> {
    state.engine.pause_import_job(job_id)
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_resume_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.resume_import_job(job_id, request_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop import resume task stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_recover_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
    recovery_override: RecoveryOverride,
    request_id: String,
) -> Result<TextDocumentImportResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.recover_import_job(job_id, recovery_override, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop import recovery task stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_cancel_import_job(
    state: State<'_, DesktopState>,
    job_id: String,
) -> Result<ImportControlResult, BridgeError> {
    state.engine.cancel_import_job(job_id)
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_cancel(
    state: State<'_, DesktopState>,
    target_request_id: String,
) -> Result<CancelResult, BridgeError> {
    state.engine.cancel(target_request_id)
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_subscribe(
    state: State<'_, DesktopState>,
    subscription_id: String,
    event_channel: Channel<BridgeEvent>,
) -> Result<(), BridgeError> {
    state.engine.subscribe(subscription_id, event_channel)
}

#[tauri::command(rename_all = "camelCase")]
fn desktop_unsubscribe(state: State<'_, DesktopState>, subscription_id: String) {
    state.engine.unsubscribe(&subscription_id);
}

fn allow_source_images(
    app: &tauri::AppHandle,
    activation: &KnowledgeBaseActivationResult,
) -> Result<(), BridgeError> {
    desktop_runtime::allow_source_image_directory(app, &activation.knowledge_base.kb_dir).map_err(
        |message| BridgeError {
            code: "desktop_source_image_scope_failed".to_owned(),
            message,
        },
    )
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, cwd| {
            desktop_runtime::forward_launch_intent(app, args, cwd);
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(DesktopState {
            engine: Arc::new(EngineSupervisor::default()),
            _process_tree: ProcessTreeJob::create()
                .expect("Could not create the OpenKB Desktop Runtime process tree"),
            runtime: DesktopRuntimeState::default(),
        })
        .setup(|app| Ok(desktop_runtime::initialize(app)?))
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.app_handle().state::<DesktopState>();
                if state.runtime.should_hide_main_window() {
                    api.prevent_close();
                    if let Err(error) = window.hide() {
                        desktop_runtime::append_application_log(
                            window.app_handle(),
                            &format!("Could not hide OpenKB Desktop window to the tray: {error}"),
                        );
                        eprintln!("Could not hide OpenKB Desktop window to the tray: {error}");
                    } else {
                        state.runtime.note_main_window_hidden();
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            desktop_bridge_handshake,
            desktop_engine_health,
            desktop_create_knowledge_base,
            desktop_open_knowledge_base,
            desktop_take_launch_intents,
            desktop_active_knowledge_base,
            desktop_reveal_knowledge_base_directory,
            desktop_reveal_application_log_directory,
            desktop_quit_application,
            desktop_open_external_url,
            desktop_inspect_import_sources,
            desktop_import_text_document,
            desktop_import_jobs,
            desktop_ask_grounded,
            desktop_retry_interrupted_answer,
            desktop_grounded_answers,
            desktop_conversations,
            desktop_global_search,
            desktop_conversation,
            desktop_create_conversation,
            desktop_rename_conversation,
            desktop_delete_conversation,
            desktop_save_conversation_draft,
            desktop_ask_conversation,
            desktop_regenerate_conversation_answer,
            desktop_select_answer_version,
            desktop_model_settings,
            desktop_save_model_settings,
            desktop_test_model_connection,
            desktop_export_diagnostic_bundle,
            desktop_knowledge_pages,
            desktop_get_knowledge_page,
            desktop_save_knowledge_page,
            desktop_document_version_candidates,
            desktop_knowledge_reconciliation_conflicts,
            desktop_stage_knowledge_reconciliation_decisions,
            desktop_commit_knowledge_reconciliation_decisions,
            desktop_resolve_document_version_candidate,
            desktop_read_raw_document,
            desktop_pause_import_job,
            desktop_resume_import_job,
            desktop_recover_import_job,
            desktop_cancel_import_job,
            desktop_cancel,
            desktop_subscribe,
            desktop_unsubscribe,
        ])
        .build(tauri::generate_context!())
        .expect("error while building OpenKB Desktop Shell");
    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            desktop_runtime::shutdown_engine(app_handle);
        }
    });
}
