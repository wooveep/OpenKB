//! Workbench commands for logging status and Sensitive Trace containment.

use crate::{desktop_logging, desktop_runtime, engine_protocol::BridgeError};

#[tauri::command]
pub(crate) fn desktop_reveal_application_log_directory(
    app: tauri::AppHandle,
) -> Result<(), BridgeError> {
    desktop_runtime::reveal_application_log_directory(&app).map_err(diagnostic_error)
}

#[tauri::command]
pub(crate) fn desktop_diagnostic_status() -> desktop_logging::DiagnosticStatus {
    desktop_logging::status()
}

#[tauri::command]
pub(crate) fn desktop_stop_sensitive_trace(
) -> Result<desktop_logging::DiagnosticStatus, BridgeError> {
    desktop_logging::stop_sensitive_trace().map_err(diagnostic_error)
}

#[tauri::command(rename_all = "camelCase")]
pub(crate) fn desktop_reveal_sensitive_trace_directory(confirmed: bool) -> Result<(), BridgeError> {
    if !confirmed {
        return Err(BridgeError::new(
            "sensitive_trace_confirmation_required",
            "Confirm that Sensitive Trace may contain document and model content.",
        ));
    }
    let directory = desktop_logging::sensitive_trace_directory().map_err(diagnostic_error)?;
    desktop_runtime::reveal_directory(&directory).map_err(diagnostic_error)
}

fn diagnostic_error(message: String) -> BridgeError {
    BridgeError::new("desktop_diagnostic_command_failed", message)
}
