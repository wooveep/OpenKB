//! Tauri commands for saving and independently verifying model-role settings.

use crate::engine_protocol::{
    BridgeError, ModelConnectionTest, ModelSettings, ModelSettingsDraft,
    SaveAndVerifyModelConfiguration,
};
use crate::DesktopState;
use std::sync::Arc;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_model_settings(
    state: State<'_, DesktopState>,
) -> Result<ModelSettings, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.model_settings())
        .await
        .map_err(join_error("lookup"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_model_settings(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<ModelSettings, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.save_model_settings(settings, request_id))
        .await
        .map_err(join_error("save"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_and_verify_model_settings(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<SaveAndVerifyModelConfiguration, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        engine.save_and_verify_model_settings(settings, request_id)
    })
    .await
    .map_err(join_error("Save and Verify"))?
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_test_model_connection(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<ModelConnectionTest, BridgeError> {
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || engine.test_model_connection(settings, request_id))
        .await
        .map_err(join_error("connection test"))?
}

fn join_error(operation: &'static str) -> impl FnOnce(tauri::Error) -> BridgeError {
    move |error| {
        BridgeError::new(
            "desktop_command_failed",
            format!("Desktop model-settings {operation} stopped unexpectedly: {error}"),
        )
    }
}
