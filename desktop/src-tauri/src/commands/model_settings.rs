//! Tauri commands for saving and independently verifying model-role settings.

use super::run_engine;
use crate::engine::protocol::{
    BridgeError, ModelConnectionTest, ModelSettings, ModelSettingsDraft,
    SaveAndVerifyModelConfiguration,
};
use crate::DesktopState;
use tauri::State;

#[tauri::command]
pub(crate) async fn desktop_model_settings(
    state: State<'_, DesktopState>,
) -> Result<ModelSettings, BridgeError> {
    run_engine(&state.engine, "model_settings", move |engine| {
        engine.model_settings()
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_model_settings(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<ModelSettings, BridgeError> {
    run_engine(&state.engine, "save_model_settings", move |engine| {
        engine.save_model_settings(settings, request_id)
    })
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_save_and_verify_model_settings(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<SaveAndVerifyModelConfiguration, BridgeError> {
    run_engine(
        &state.engine,
        "save_and_verify_model_settings",
        move |engine| engine.save_and_verify_model_settings(settings, request_id),
    )
    .await
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) async fn desktop_test_model_connection(
    state: State<'_, DesktopState>,
    settings: ModelSettingsDraft,
    request_id: String,
) -> Result<ModelConnectionTest, BridgeError> {
    run_engine(&state.engine, "test_model_connection", move |engine| {
        engine.test_model_connection(settings, request_id)
    })
    .await
}
