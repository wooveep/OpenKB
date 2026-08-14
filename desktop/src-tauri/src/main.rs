//! OpenKB Desktop Shell: native window ownership and typed Engine mediation.

mod engine_protocol;
mod engine_wire;
mod process_tree;

use engine_protocol::{
    ActiveKnowledgeBaseResult, BridgeError, BridgeEvent, BridgeHandshake, CancelResult,
    EngineHealth, EngineSupervisor, ImportControlResult, ImportJobsResult, ImportSourceInspection,
    InspectKnowledgeBaseResult, KnowledgeBaseActivationResult, RecoveryOverride,
    TextDocumentImportResult,
};
use process_tree::ProcessTreeJob;
use std::sync::Arc;
use tauri::{ipc::Channel, Manager, State};

struct DesktopState {
    engine: Arc<EngineSupervisor>,
    _process_tree: ProcessTreeJob,
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
async fn desktop_inspect_knowledge_base(
    state: State<'_, DesktopState>,
    kb_dir: String,
    request_id: String,
) -> Result<InspectKnowledgeBaseResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.inspect_knowledge_base(kb_dir, request_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop inspection task stopped unexpectedly: {error}"),
        })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_create_knowledge_base(
    state: State<'_, DesktopState>,
    kb_dir: String,
    name: Option<String>,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.create_knowledge_base(kb_dir, name, request_id)
    })
    .await
    .map_err(|error| BridgeError {
        code: "desktop_command_failed".to_owned(),
        message: format!("Desktop knowledge-base creation task stopped unexpectedly: {error}"),
    })?
}

#[tauri::command(rename_all = "camelCase")]
async fn desktop_open_knowledge_base(
    state: State<'_, DesktopState>,
    kb_dir: String,
    request_id: String,
) -> Result<KnowledgeBaseActivationResult, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.open_knowledge_base(kb_dir, request_id))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop knowledge-base open task stopped unexpectedly: {error}"),
        })?
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
        message: format!("Desktop TXT import task stopped unexpectedly: {error}"),
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

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(DesktopState {
            engine: Arc::new(EngineSupervisor::default()),
            _process_tree: ProcessTreeJob::create()
                .expect("Could not create the OpenKB Desktop Runtime process tree"),
        })
        .setup(|app| {
            // Do not hold window creation behind a Python startup: React renders
            // its starting state immediately and asks the same supervisor for the
            // handshake. The supervisor serializes either path to one Engine.
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let state = app_handle.state::<DesktopState>();
                if let Err(error) = state.engine.start() {
                    eprintln!(
                        "OpenKB Desktop Engine did not start during shell setup: {}",
                        error.message
                    );
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_bridge_handshake,
            desktop_engine_health,
            desktop_inspect_knowledge_base,
            desktop_create_knowledge_base,
            desktop_open_knowledge_base,
            desktop_active_knowledge_base,
            desktop_inspect_import_sources,
            desktop_import_text_document,
            desktop_import_jobs,
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
            app_handle.state::<DesktopState>().engine.shutdown();
        }
    });
}
