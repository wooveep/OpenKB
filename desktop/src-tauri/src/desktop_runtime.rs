//! Desktop Shell lifecycle: tray behavior, single-instance intents, and Engine recovery.

use crate::DesktopState;
use serde::Serialize;
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{TrayIcon, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Emitter, Manager,
};

const ACTIVE_KNOWLEDGE_BASE_FILE: &str = "active-knowledge-base.txt";
const OPEN_MENU_ID: &str = "desktop.open";
const TASKS_MENU_ID: &str = "desktop.tasks";
const QUIT_MENU_ID: &str = "desktop.quit";
const LAUNCH_INTENTS_READY_EVENT: &str = "desktop://launch-intents-ready";
const TASK_CENTER_EVENT: &str = "desktop://task-center";

pub(crate) struct DesktopRuntimeState {
    explicit_exit: AtomicBool,
    pending_launch_intents: Mutex<Vec<DesktopLaunchIntent>>,
    tray: Mutex<Option<TrayIcon>>,
}

impl Default for DesktopRuntimeState {
    fn default() -> Self {
        Self {
            explicit_exit: AtomicBool::new(false),
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
}

pub(crate) fn initialize(app: &App) -> tauri::Result<()> {
    let app_handle = app.handle().clone();
    let state = app.state::<DesktopState>();
    if let Some(kb_dir) = load_active_knowledge_base(&app_handle) {
        state.engine.remember_active_knowledge_base(kb_dir);
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
        eprintln!("Could not remember active OpenKB Desktop Knowledge Base: {error}");
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
            if matches!(event, TrayIconEvent::Click { .. }) {
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
        if let Err(error) = state.engine.start() {
            eprintln!(
                "OpenKB Desktop Engine did not start during shell setup: {}",
                error.message
            );
        }
    });
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(250));
        let state = app.state::<DesktopState>();
        if !state.runtime.should_hide_main_window() {
            return;
        }
        if state.engine.restart_after_unexpected_exit() {
            let _ = app.emit("desktop://engine-restarted", ());
        }
    });
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
    app.path()
        .app_data_dir()
        .ok()
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

#[cfg(test)]
mod tests {
    use super::launch_paths;
    use std::{fs, time::SystemTime};

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
