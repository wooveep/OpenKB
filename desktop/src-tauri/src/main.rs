#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

//! OpenKB Desktop Shell: native window ownership and typed Engine mediation.

mod commands;
mod diagnostics;
mod engine;
mod runtime;
use commands::semantic_review::{desktop_resolve_semantic_review, desktop_semantic_reviews};

use commands::diagnostic::{
    desktop_diagnostic_status, desktop_reveal_application_log_directory,
    desktop_reveal_sensitive_trace_directory, desktop_stop_sensitive_trace,
};
use commands::document_version::{
    desktop_confirm_document_lineage, desktop_document_version_candidates,
    desktop_document_version_catalog, desktop_document_version_diffs,
    desktop_resolve_document_version_candidate,
};
use commands::knowledge_page::{
    desktop_adopt_knowledge_item, desktop_bind_knowledge_page_source,
    desktop_deprecate_knowledge_page, desktop_export_knowledge_bundle, desktop_get_knowledge_page,
    desktop_get_knowledge_workspace_item, desktop_knowledge_pages, desktop_knowledge_workspace,
    desktop_knowledge_workspace_history, desktop_permanently_delete_knowledge_page,
    desktop_preview_knowledge_bundle, desktop_publish_knowledge_page,
    desktop_restore_knowledge_page, desktop_save_knowledge_page, desktop_search_knowledge_sources,
    desktop_set_knowledge_page_stale_after, desktop_verify_knowledge_page,
};
use commands::knowledge_reanalysis::{
    desktop_knowledge_reanalysis, desktop_retry_knowledge_reanalysis,
    desktop_start_knowledge_reanalysis,
};
use commands::missing_source::{
    desktop_bind_missing_source_candidate, desktop_dismiss_missing_source_candidates,
    desktop_missing_source_candidates,
};
use commands::model_settings::{
    desktop_model_settings, desktop_save_and_verify_model_settings, desktop_save_model_settings,
    desktop_test_model_connection,
};
use commands::workbench::*;
use engine::protocol::EngineSupervisor;
use runtime::process_tree::ProcessTreeJob;
use runtime::DesktopRuntimeState;
use std::sync::Arc;
use tauri::Manager;

pub(crate) struct DesktopState {
    pub(crate) engine: Arc<EngineSupervisor>,
    _process_tree: ProcessTreeJob,
    pub(crate) runtime: DesktopRuntimeState,
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, cwd| {
            runtime::forward_launch_intent(app, args, cwd);
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(DesktopState {
            engine: Arc::new(EngineSupervisor::default()),
            _process_tree: ProcessTreeJob::create()
                .expect("Could not create the OpenKB Desktop Runtime process tree"),
            runtime: DesktopRuntimeState::default(),
        })
        .setup(|app| Ok(runtime::initialize(app)?))
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.app_handle().state::<DesktopState>();
                if state.runtime.should_hide_main_window() {
                    api.prevent_close();
                    if let Err(error) = window.hide() {
                        diagnostics::logging::event(
                            diagnostics::config::LogLevel::Warn,
                            "shell",
                            "window_hide_failed",
                            "The main window could not be hidden to the tray.",
                            serde_json::json!({"error_code": "window_hide_failed", "error_type": std::any::type_name_of_val(&error)}),
                        );
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
            desktop_diagnostic_status,
            desktop_stop_sensitive_trace,
            desktop_reveal_sensitive_trace_directory,
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
            desktop_save_and_verify_model_settings,
            desktop_test_model_connection,
            desktop_export_diagnostic_bundle,
            desktop_knowledge_reanalysis,
            desktop_start_knowledge_reanalysis,
            desktop_retry_knowledge_reanalysis,
            desktop_knowledge_workspace,
            desktop_get_knowledge_workspace_item,
            desktop_knowledge_workspace_history,
            desktop_adopt_knowledge_item,
            desktop_knowledge_pages,
            desktop_get_knowledge_page,
            desktop_save_knowledge_page,
            desktop_publish_knowledge_page,
            desktop_verify_knowledge_page,
            desktop_set_knowledge_page_stale_after,
            desktop_deprecate_knowledge_page,
            desktop_restore_knowledge_page,
            desktop_permanently_delete_knowledge_page,
            desktop_search_knowledge_sources,
            desktop_bind_knowledge_page_source,
            desktop_preview_knowledge_bundle,
            desktop_export_knowledge_bundle,
            desktop_document_version_candidates,
            desktop_document_version_catalog,
            desktop_confirm_document_lineage,
            desktop_document_version_diffs,
            desktop_knowledge_reconciliation_conflicts,
            desktop_semantic_reviews,
            desktop_resolve_semantic_review,
            desktop_stage_knowledge_reconciliation_decisions,
            desktop_commit_knowledge_reconciliation_decisions,
            desktop_missing_source_candidates,
            desktop_bind_missing_source_candidate,
            desktop_dismiss_missing_source_candidates,
            desktop_resolve_document_version_candidate,
            desktop_read_raw_document,
            desktop_pause_import_job,
            desktop_resume_import_job,
            desktop_recover_import_job,
            desktop_cancel_import_job,
            desktop_cancel_page_tree_enrichment,
            desktop_retry_page_tree_enrichment,
            desktop_cancel_knowledge_graph_extraction,
            desktop_retry_knowledge_graph_extraction,
            desktop_cancel,
            desktop_subscribe,
            desktop_unsubscribe,
        ])
        .build(tauri::generate_context!())
        .expect("error while building OpenKB Desktop Shell");
    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            runtime::shutdown_engine(app_handle);
        }
    });
}
