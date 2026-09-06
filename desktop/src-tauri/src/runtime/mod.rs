//! Desktop Shell lifecycle: tray behavior, single-instance intents, and Engine recovery.

pub(crate) mod external_url;
pub(crate) mod process_tree;

use crate::diagnostics;
use crate::{diagnostics::config::LogLevel, DesktopState};
use serde::Serialize;
use serde_json::json;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Emitter, Manager,
};

const ACTIVE_KNOWLEDGE_BASE_FILE: &str = "active-knowledge-base.txt";
// Packaging harnesses set this explicitly so shell smoke tests cannot restore a real user KB.
const RUNTIME_DIRECTORY_OVERRIDE: &str = "OPENKB_DESKTOP_RUNTIME_DIR";
const OPEN_MENU_ID: &str = "desktop.open";
const TASKS_MENU_ID: &str = "desktop.tasks";
const QUIT_MENU_ID: &str = "desktop.quit";
const LAUNCH_INTENTS_READY_EVENT: &str = "desktop://launch-intents-ready";
const TASK_CENTER_EVENT: &str = "desktop://task-center";
const TRAY_RESTORED_EVENT: &str = "desktop://tray-restored";
const ACTIVE_KNOWLEDGE_BASE_RESTORED_EVENT: &str = "desktop://active-knowledge-base-restored";

pub(crate) fn allow_source_image_directory(
    app: &AppHandle,
    knowledge_base_dir: &str,
) -> Result<(), String> {
    let image_dir = Path::new(knowledge_base_dir)
        .join("derived")
        .join("source-images");
    app.asset_protocol_scope()
        .allow_directory(image_dir, true)
        .map_err(|error| format!("Could not enable source images for this knowledge base: {error}"))
}

pub(crate) struct DesktopRuntimeState {
    explicit_exit: AtomicBool,
    hidden_to_tray: AtomicBool,
    pending_launch_intents: Mutex<Vec<DesktopLaunchIntent>>,
    tray: Mutex<Option<TrayIcon>>,
}

impl Default for DesktopRuntimeState {
    fn default() -> Self {
        Self {
            explicit_exit: AtomicBool::new(false),
            hidden_to_tray: AtomicBool::new(false),
            pending_launch_intents: Mutex::new(Vec::new()),
            tray: Mutex::new(None),
        }
    }
}

impl DesktopRuntimeState {
    pub(crate) fn should_hide_main_window(&self) -> bool {
        !self.explicit_exit.load(Ordering::Acquire)
    }

    fn begin_explicit_exit(&self) -> bool {
        !self.explicit_exit.swap(true, Ordering::AcqRel)
    }

    pub(crate) fn note_main_window_hidden(&self) {
        self.hidden_to_tray.store(true, Ordering::Release);
    }

    fn take_tray_restore_notice(&self) -> bool {
        self.hidden_to_tray.swap(false, Ordering::AcqRel)
    }

    fn retain_tray(&self, tray: TrayIcon) {
        if let Ok(mut retained) = self.tray.lock() {
            *retained = Some(tray);
        }
    }

    fn enqueue_launch_intents(&self, intents: Vec<DesktopLaunchIntent>) {
        if let Ok(mut pending) = self.pending_launch_intents.lock() {
            pending.extend(intents);
        }
    }

    pub(crate) fn take_launch_intents(&self) -> Vec<DesktopLaunchIntent> {
        self.pending_launch_intents
            .lock()
            .map(|mut pending| std::mem::take(&mut *pending))
            .unwrap_or_default()
    }
}

#[derive(Clone, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
pub(crate) enum DesktopLaunchIntent {
    OpenKnowledgeBase { kb_dir: String },
    ImportSources { source_paths: Vec<String> },
    PreviousKnowledgeBaseUnavailable { kb_dir: String },
    ActiveKnowledgeBaseRestored,
}

pub(crate) fn initialize(app: &App) -> tauri::Result<()> {
    let app_handle = app.handle().clone();
    let _ = diagnostics::logging::initialize(&app_handle);
    let state = app.state::<DesktopState>();
    if let Some(kb_dir) = load_active_knowledge_base(&app_handle) {
        if is_desktop_knowledge_base(Path::new(&kb_dir)) {
            state.engine.remember_active_knowledge_base(kb_dir);
        } else {
            clear_active_knowledge_base(&app_handle);
            state.runtime.enqueue_launch_intents(vec![
                DesktopLaunchIntent::PreviousKnowledgeBaseUnavailable { kb_dir },
            ]);
        }
    }
    install_tray(app)?;
    start_engine_supervision(app_handle);
    Ok(())
}

pub(crate) fn remember_active_knowledge_base(app: &AppHandle, kb_dir: &str) {
    app.state::<DesktopState>()
        .engine
        .remember_active_knowledge_base(kb_dir.to_owned());
    if let Err(error) = persist_active_knowledge_base(app, kb_dir) {
        diagnostics::logging::event(
            LogLevel::Warn,
            "storage",
            "active_knowledge_base_persist_failed",
            "The active knowledge-base pointer could not be persisted.",
            json!({"error_code": "active_knowledge_base_persist_failed", "error_type": error.kind().to_string()}),
        );
    }
}

pub(crate) fn request_explicit_exit(app: &AppHandle) {
    shutdown_engine(app);
    app.exit(0);
}

pub(crate) fn shutdown_engine(app: &AppHandle) {
    let state = app.state::<DesktopState>();
    if state.runtime.begin_explicit_exit() {
        state.engine.shutdown();
    }
}

pub(crate) fn forward_launch_intent(app: &AppHandle, args: Vec<String>, cwd: String) {
    show_main_window(app);
    let cwd = PathBuf::from(cwd);
    let (knowledge_bases, source_paths) = launch_paths(args, &cwd);
    let mut intents = Vec::new();
    if let Some(kb_dir) = knowledge_bases.into_iter().next() {
        intents.push(DesktopLaunchIntent::OpenKnowledgeBase { kb_dir });
    }
    if !source_paths.is_empty() {
        intents.push(DesktopLaunchIntent::ImportSources { source_paths });
    }
    if !intents.is_empty() {
        app.state::<DesktopState>()
            .runtime
            .enqueue_launch_intents(intents);
        let _ = app.emit(LAUNCH_INTENTS_READY_EVENT, ());
    }
}

pub(crate) fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
    if app
        .state::<DesktopState>()
        .runtime
        .take_tray_restore_notice()
    {
        let _ = app.emit(TRAY_RESTORED_EVENT, ());
    }
}

fn install_tray(app: &App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, OPEN_MENU_ID, "Open OpenKB", true, None::<&str>)?;
    let tasks = MenuItem::with_id(app, TASKS_MENU_ID, "Show tasks", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, QUIT_MENU_ID, "Quit OpenKB", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &tasks, &quit])?;
    let mut builder = TrayIconBuilder::with_id("openkb-tray")
        .menu(&menu)
        .tooltip("OpenKB")
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            OPEN_MENU_ID => show_main_window(app),
            TASKS_MENU_ID => {
                show_main_window(app);
                let _ = app.emit(TASK_CENTER_EVENT, ());
            }
            QUIT_MENU_ID => request_explicit_exit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    let tray = builder.build(app)?;
    app.state::<DesktopState>().runtime.retain_tray(tray);
    Ok(())
}

fn start_engine_supervision(app: AppHandle) {
    let startup_handle = app.clone();
    thread::spawn(move || {
        let state = startup_handle.state::<DesktopState>();
        let started_at = Instant::now();
        diagnostics::logging::event(
            LogLevel::Debug,
            "runtime",
            "engine_start_initiated",
            "Desktop Shell initiated Engine startup.",
            json!({}),
        );
        if let Err(error) = state.engine.start() {
            diagnostics::logging::event(
                LogLevel::Error,
                "runtime",
                "engine_start_failed",
                "Desktop Engine did not start during Shell setup.",
                json!({"error_code": error.code, "outcome": "failed"}),
            );
            return;
        }
        diagnostics::logging::event(
            LogLevel::Info,
            "runtime",
            "engine_handshake_completed",
            "Desktop Engine handshake completed.",
            json!({"elapsed_ms": started_at.elapsed().as_millis(), "outcome": "succeeded"}),
        );
        restore_active_knowledge_base(&startup_handle, "startup");
    });
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(250));
        let restarted = {
            let state = app.state::<DesktopState>();
            if !state.runtime.should_hide_main_window() {
                return;
            }
            state.engine.restart_after_unexpected_exit()
        };
        if restarted {
            diagnostics::logging::event(
                LogLevel::Warn,
                "runtime",
                "engine_restarted",
                "Desktop Engine restarted after an unexpected exit.",
                json!({"outcome": "restarted"}),
            );
            restore_active_knowledge_base(&app, "restart");
            let _ = app.emit("desktop://engine-restarted", ());
        }
    });
}

fn restore_active_knowledge_base(app: &AppHandle, reason: &str) {
    let started_at = Instant::now();
    match app
        .state::<DesktopState>()
        .engine
        .restore_active_knowledge_base()
    {
        Ok(true) => {
            if let Some(kb_dir) = app
                .state::<DesktopState>()
                .engine
                .remembered_active_knowledge_base()
            {
                if allow_source_image_directory(app, &kb_dir).is_err() {
                    diagnostics::logging::event(
                        LogLevel::Warn,
                        "runtime",
                        "source_image_scope_restore_failed",
                        "The source-image scope could not be restored.",
                        json!({"error_code": "source_image_scope_restore_failed"}),
                    );
                }
            }
            diagnostics::logging::event(
                LogLevel::Info,
                "runtime",
                "active_knowledge_base_restored",
                "The active knowledge base was restored.",
                json!({"phase": reason, "elapsed_ms": started_at.elapsed().as_millis(), "outcome": "succeeded"}),
            );
            app.state::<DesktopState>()
                .runtime
                .enqueue_launch_intents(vec![DesktopLaunchIntent::ActiveKnowledgeBaseRestored]);
            let _ = app.emit(ACTIVE_KNOWLEDGE_BASE_RESTORED_EVENT, ());
        }
        Ok(false) => {
            diagnostics::logging::event(
                LogLevel::Debug,
                "runtime",
                "active_knowledge_base_restore_skipped",
                "No active knowledge base required restoration.",
                json!({"phase": reason, "outcome": "skipped"}),
            );
        }
        Err(error) => {
            diagnostics::logging::event(
                LogLevel::Warn,
                "runtime",
                "active_knowledge_base_restore_failed",
                "The active knowledge base could not be restored.",
                json!({"phase": reason, "error_code": error.code, "outcome": "failed"}),
            );
        }
    }
}

fn launch_paths(args: Vec<String>, cwd: &Path) -> (Vec<String>, Vec<String>) {
    let mut knowledge_bases = Vec::new();
    let mut source_paths = Vec::new();
    for argument in args
        .into_iter()
        .skip(1)
        .filter(|argument| !argument.starts_with('-'))
    {
        let path = PathBuf::from(argument);
        let path = if path.is_absolute() {
            path
        } else {
            cwd.join(path)
        };
        if !path.exists() {
            continue;
        }
        let path = path.to_string_lossy().into_owned();
        if is_desktop_knowledge_base(Path::new(&path)) {
            knowledge_bases.push(path);
        } else {
            source_paths.push(path);
        }
    }
    (knowledge_bases, source_paths)
}

fn is_desktop_knowledge_base(path: &Path) -> bool {
    path.join(".openkb").join("state.sqlite3").is_file()
}

fn active_knowledge_base_file(app: &AppHandle) -> Option<PathBuf> {
    let override_directory = env::var_os(RUNTIME_DIRECTORY_OVERRIDE)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    active_knowledge_base_file_for(override_directory, app.path().app_data_dir().ok())
}

fn active_knowledge_base_file_for(
    override_directory: Option<PathBuf>,
    app_data_directory: Option<PathBuf>,
) -> Option<PathBuf> {
    override_directory
        .or(app_data_directory)
        .map(|directory| directory.join(ACTIVE_KNOWLEDGE_BASE_FILE))
}

fn load_active_knowledge_base(app: &AppHandle) -> Option<String> {
    let path = active_knowledge_base_file(app)?;
    let kb_dir = fs::read_to_string(path).ok()?;
    let kb_dir = kb_dir.trim();
    (!kb_dir.is_empty()).then(|| kb_dir.to_owned())
}

fn persist_active_knowledge_base(app: &AppHandle, kb_dir: &str) -> std::io::Result<()> {
    let Some(path) = active_knowledge_base_file(app) else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, kb_dir)
}

fn clear_active_knowledge_base(app: &AppHandle) {
    let Some(path) = active_knowledge_base_file(app) else {
        return;
    };
    if let Err(error) = fs::remove_file(path) {
        if error.kind() != std::io::ErrorKind::NotFound {
            diagnostics::logging::event(
                LogLevel::Warn,
                "storage",
                "active_knowledge_base_clear_failed",
                "An unavailable active knowledge-base pointer could not be cleared.",
                json!({"error_code": "active_knowledge_base_clear_failed", "error_type": error.kind().to_string()}),
            );
        }
    }
}

pub(crate) fn reveal_directory(directory: &Path) -> Result<(), String> {
    if !directory.is_dir() {
        return Err(format!("Directory is unavailable: {}", directory.display()));
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

        let mut command = Command::new("explorer.exe");
        command.arg(directory).creation_flags(CREATE_NO_WINDOW);
        command
            .spawn()
            .map_err(|error| format!("Could not open directory in Explorer: {error}"))?;
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(directory)
            .spawn()
            .map_err(|error| format!("Could not open directory: {error}"))?;
        return Ok(());
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(directory)
            .spawn()
            .map_err(|error| format!("Could not open directory: {error}"))?;
        Ok(())
    }
}

pub(crate) fn reveal_application_log_directory(_app: &AppHandle) -> Result<(), String> {
    let directory = diagnostics::logging::application_log_directory()?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create OpenKB log directory: {error}"))?;
    reveal_directory(&directory)
}

#[cfg(test)]
mod tests {
    use super::{active_knowledge_base_file_for, launch_paths, ACTIVE_KNOWLEDGE_BASE_FILE};
    use serde_json::Value;
    use std::{fs, time::SystemTime};

    #[test]
    fn default_capability_allows_diagnostic_bundle_save_dialog() {
        let capability: Value =
            serde_json::from_str(include_str!("../../capabilities/default.json"))
                .expect("parse default Desktop capability");
        let permissions = capability["permissions"]
            .as_array()
            .expect("default capability permissions");

        assert!(
            permissions
                .iter()
                .any(|permission| permission == "dialog:allow-save"),
            "diagnostic bundle export requires dialog:allow-save"
        );
    }

    #[test]
    fn explicit_runtime_directory_isolates_active_knowledge_base_state() {
        let persistent = std::path::PathBuf::from("persistent-runtime");
        let isolated = std::path::PathBuf::from("isolated-runtime");

        assert_eq!(
            active_knowledge_base_file_for(Some(isolated.clone()), Some(persistent.clone())),
            Some(isolated.join(ACTIVE_KNOWLEDGE_BASE_FILE))
        );
        assert_eq!(
            active_knowledge_base_file_for(None, Some(persistent.clone())),
            Some(persistent.join(ACTIVE_KNOWLEDGE_BASE_FILE))
        );
        assert_eq!(active_knowledge_base_file_for(None, None), None);
    }

    #[test]
    fn launch_paths_classify_desktop_knowledge_base_and_import_sources() {
        let root = std::env::temp_dir().join(format!(
            "openkb-desktop-runtime-{}",
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .expect("wall clock after epoch")
                .as_nanos()
        ));
        let knowledge_base = root.join("knowledge-base");
        let document = root.join("guide.txt");
        fs::create_dir_all(knowledge_base.join(".openkb")).expect("create knowledge base state");
        fs::write(knowledge_base.join(".openkb/state.sqlite3"), b"").expect("write state marker");
        fs::write(&document, b"guide").expect("write source document");

        let (knowledge_bases, sources) = launch_paths(
            vec![
                "OpenKB.exe".to_owned(),
                knowledge_base.to_string_lossy().into_owned(),
                document.to_string_lossy().into_owned(),
            ],
            &root,
        );

        assert_eq!(
            knowledge_bases,
            vec![knowledge_base.to_string_lossy().into_owned()]
        );
        assert_eq!(sources, vec![document.to_string_lossy().into_owned()]);
        fs::remove_dir_all(root).expect("remove test directory");
    }
}
