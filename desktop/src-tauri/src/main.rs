//! OpenKB Desktop Shell: native window ownership and typed Engine mediation.

mod engine_protocol;
mod engine_wire;

use engine_protocol::{
    BridgeError, BridgeEvent, BridgeHandshake, CancelResult, EngineHealth, EngineSupervisor,
    InspectKnowledgeBaseResult,
};
use std::sync::Arc;
use tauri::{ipc::Channel, Manager, State};

struct DesktopState {
    engine: Arc<EngineSupervisor>,
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
        .manage(DesktopState {
            engine: Arc::new(EngineSupervisor::default()),
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
