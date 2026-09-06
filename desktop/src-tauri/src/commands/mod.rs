//! Tauri commands and shared execution policy.

pub(crate) mod diagnostic;
pub(crate) mod document_version;
pub(crate) mod knowledge_page;
pub(crate) mod knowledge_reanalysis;
pub(crate) mod missing_source;
pub(crate) mod model_settings;
pub(crate) mod semantic_review;
pub(crate) mod workbench;

use crate::engine::protocol::{BridgeError, EngineSupervisor};
use std::sync::Arc;

/// Engine requests can wait on stdio. Keep that wait off the IPC executor.
pub(crate) async fn run_engine<T, F>(
    engine: &Arc<EngineSupervisor>,
    operation: &'static str,
    work: F,
) -> Result<T, BridgeError>
where
    T: Send + 'static,
    F: FnOnce(&EngineSupervisor) -> Result<T, BridgeError> + Send + 'static,
{
    let engine = Arc::clone(engine);
    tauri::async_runtime::spawn_blocking(move || work(&engine))
        .await
        .map_err(|error| BridgeError {
            code: "desktop_command_failed".to_owned(),
            message: format!("Desktop {operation} task stopped unexpectedly: {error}"),
        })?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engine_work_runs_off_the_calling_thread() {
        let engine = Arc::new(EngineSupervisor::default());
        let caller = std::thread::current().id();
        let worker = tauri::async_runtime::block_on(run_engine(&engine, "test", |_| {
            Ok(std::thread::current().id())
        }))
        .unwrap();
        assert_ne!(worker, caller);
    }

    #[test]
    fn engine_errors_keep_their_domain_code() {
        let engine = Arc::new(EngineSupervisor::default());
        let result: Result<(), BridgeError> =
            tauri::async_runtime::block_on(run_engine(&engine, "test", |_| {
                Err(BridgeError::new("import_paused", "Paused by user."))
            }));
        assert_eq!(result.unwrap_err().code, "import_paused");
    }

    #[test]
    fn worker_panics_become_command_errors() {
        let engine = Arc::new(EngineSupervisor::default());
        let result: Result<(), BridgeError> =
            tauri::async_runtime::block_on(run_engine(&engine, "test", |_| {
                panic!("test worker panic")
            }));
        assert_eq!(result.unwrap_err().code, "desktop_command_failed");
    }
}
