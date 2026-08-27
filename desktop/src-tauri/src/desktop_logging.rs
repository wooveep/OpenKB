//! Versioned support-safe Shell logging and diagnostic-session control.

use crate::desktop_logging_config::{load_logging_config, LogLevel, LoggingConfig};
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::{
    collections::VecDeque,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{ChildStderr, Command},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Condvar, Mutex, OnceLock,
    },
    thread,
    time::{SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Manager};

const SHELL_LOG_FILE: &str = "openkb-shell.log";
const MAX_LOG_BYTES: u64 = 10 * 1024 * 1024;
const LOG_BACKUPS: usize = 4;
const EVENT_QUEUE_CAPACITY: usize = 2_048;
const SCHEMA_VERSION: u64 = 1;
static DIAGNOSTICS: OnceLock<Arc<ShellDiagnostics>> = OnceLock::new();

#[derive(Clone)]
struct PendingEvent {
    level: LogLevel,
    component: String,
    event: String,
    summary: String,
    fields: Map<String, Value>,
}

struct ShellLogWriter {
    path: PathBuf,
    runtime_session_id: String,
    sequence: AtomicU64,
    dropped_trace: AtomicU64,
    dropped_debug: AtomicU64,
    dropped_info: AtomicU64,
    lock: Mutex<()>,
}

struct ShellDiagnostics {
    config: LoggingConfig,
    writer: Arc<ShellLogWriter>,
    queue: Arc<BoundedEventQueue>,
}

struct BoundedEventQueue {
    capacity: usize,
    events: Mutex<VecDeque<PendingEvent>>,
    available: Condvar,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticStatus {
    configured_level: String,
    effective_level: String,
    configured_components: Map<String, Value>,
    effective_components: Map<String, Value>,
    warnings: Vec<String>,
    configuration_file: String,
    sensitive_trace_active: bool,
    sensitive_trace_capture_id: Option<String>,
    sensitive_trace_expires_at: Option<String>,
    sensitive_trace_size_bytes: u64,
    trace_components: Vec<String>,
}

pub(crate) fn initialize(app: &AppHandle) -> Result<(), String> {
    if DIAGNOSTICS.get().is_some() {
        return Ok(());
    }
    let local_state_root = local_state_root(app)?;
    let config = load_logging_config(local_state_root);
    fs::create_dir_all(&config.log_directory)
        .map_err(|error| format!("Could not create OpenKB log directory: {error}"))?;
    let path = config.log_directory.join(SHELL_LOG_FILE);
    migrate_legacy_logs(&path);
    let writer = Arc::new(ShellLogWriter {
        path,
        runtime_session_id: config.runtime_session_id.clone(),
        sequence: AtomicU64::new(0),
        dropped_trace: AtomicU64::new(0),
        dropped_debug: AtomicU64::new(0),
        dropped_info: AtomicU64::new(0),
        lock: Mutex::new(()),
    });
    let queue = Arc::new(BoundedEventQueue {
        capacity: EVENT_QUEUE_CAPACITY,
        events: Mutex::new(VecDeque::new()),
        available: Condvar::new(),
    });
    let worker_writer = Arc::clone(&writer);
    let worker_queue = Arc::clone(&queue);
    thread::Builder::new()
        .name("openkb-shell-log-writer".to_owned())
        .spawn(move || {
            while let Some(event) = worker_queue.pop() {
                worker_writer.write(event);
            }
        })
        .map_err(|error| format!("Could not start OpenKB log writer: {error}"))?;
    DIAGNOSTICS
        .set(Arc::new(ShellDiagnostics {
            config,
            writer,
            queue,
        }))
        .map_err(|_| "OpenKB diagnostics were initialized twice.".to_owned())?;

    if let Some(diagnostics) = DIAGNOSTICS.get() {
        for warning in &diagnostics.config.warnings {
            let effective_level = diagnostics.config.effective_level("runtime").as_str();
            event(
                LogLevel::Warn,
                "runtime",
                "logging_configuration_warning",
                "Desktop logging configuration was normalized with a warning.",
                json!({"warning_code": warning, "effective_level": effective_level}),
            );
        }
    }
    event(
        LogLevel::Info,
        "shell",
        "shell_started",
        "OpenKB Desktop Shell started.",
        json!({}),
    );
    Ok(())
}

pub(crate) fn event(
    level: LogLevel,
    component: &str,
    event_name: &str,
    summary: &str,
    fields: Value,
) {
    let Some(diagnostics) = DIAGNOSTICS.get() else {
        return;
    };
    if level < diagnostics.config.effective_level(component) {
        return;
    }
    let pending = PendingEvent {
        level,
        component: component.to_owned(),
        event: safe_event_name(event_name),
        summary: summary.chars().take(512).collect(),
        fields: sanitize_fields(fields),
    };
    if level >= LogLevel::Warn {
        diagnostics.writer.write(pending);
        return;
    }
    if let Some(dropped) = diagnostics.queue.push(pending) {
        diagnostics.writer.note_drop(dropped);
    }
}

impl BoundedEventQueue {
    fn push(&self, event: PendingEvent) -> Option<LogLevel> {
        let Ok(mut events) = self.events.lock() else {
            return Some(event.level);
        };
        if events.len() < self.capacity {
            events.push_back(event);
            self.available.notify_one();
            return None;
        }
        let replace_index = [LogLevel::Trace, LogLevel::Debug]
            .into_iter()
            .take_while(|preferred| event.level > *preferred)
            .find_map(|preferred| events.iter().position(|queued| queued.level == preferred));
        let Some(index) = replace_index else {
            return Some(event.level);
        };
        let dropped = events.remove(index).map(|queued| queued.level);
        events.push_back(event);
        self.available.notify_one();
        dropped
    }

    fn pop(&self) -> Option<PendingEvent> {
        let mut events = self.events.lock().ok()?;
        while events.is_empty() {
            events = self.available.wait(events).ok()?;
        }
        events.pop_front()
    }
}

pub(crate) fn configure_engine_command(command: &mut Command) {
    if let Some(diagnostics) = DIAGNOSTICS.get() {
        for (key, value) in diagnostics.config.engine_environment() {
            command.env(key, value);
        }
    } else {
        command.env("OPENKB_LOG_LEVEL", "WARN");
    }
}

pub(crate) fn status() -> DiagnosticStatus {
    let Some(diagnostics) = DIAGNOSTICS.get() else {
        return DiagnosticStatus {
            configured_level: "WARN".to_owned(),
            effective_level: "WARN".to_owned(),
            configured_components: Map::new(),
            effective_components: Map::new(),
            warnings: vec!["logging_not_initialized".to_owned()],
            configuration_file: String::new(),
            sensitive_trace_active: false,
            sensitive_trace_capture_id: None,
            sensitive_trace_expires_at: None,
            sensitive_trace_size_bytes: 0,
            trace_components: Vec::new(),
        };
    };
    let config = &diagnostics.config;
    let configured_components = config
        .components
        .iter()
        .map(|(component, level)| (component.clone(), Value::from(level.as_str())))
        .collect();
    let effective_components = crate::desktop_logging_config::DIAGNOSTIC_COMPONENTS
        .iter()
        .map(|component| {
            (
                (*component).to_owned(),
                Value::from(config.effective_level(component).as_str()),
            )
        })
        .collect();
    let capture_directory = config
        .sensitive_trace_capture_id
        .as_ref()
        .map(|capture_id| config.sensitive_trace_root.join(capture_id));
    DiagnosticStatus {
        configured_level: config.level.as_str().to_owned(),
        effective_level: config.effective_level("shell").as_str().to_owned(),
        configured_components,
        effective_components,
        warnings: config.warnings.clone(),
        configuration_file: config.config_path.to_string_lossy().into_owned(),
        sensitive_trace_active: config.sensitive_trace_active(),
        sensitive_trace_capture_id: config.sensitive_trace_capture_id.clone(),
        sensitive_trace_expires_at: config.sensitive_trace_expires_at_text.clone(),
        sensitive_trace_size_bytes: capture_directory
            .as_deref()
            .map(directory_size)
            .unwrap_or(0),
        trace_components: config.trace_components(),
    }
}

pub(crate) fn stop_sensitive_trace() -> Result<DiagnosticStatus, String> {
    let diagnostics = DIAGNOSTICS
        .get()
        .ok_or_else(|| "OpenKB diagnostics are unavailable.".to_owned())?;
    let stop_file = diagnostics
        .config
        .sensitive_trace_stop_file
        .as_ref()
        .ok_or_else(|| "No Sensitive Trace Capture is configured.".to_owned())?;
    if let Some(parent) = stop_file.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("Could not stop Sensitive Trace Capture: {error}"))?;
    }
    File::create(stop_file)
        .map_err(|error| format!("Could not stop Sensitive Trace Capture: {error}"))?;
    event(
        LogLevel::Warn,
        "runtime",
        "sensitive_trace_stopped",
        "Sensitive Trace Capture stopped for this runtime.",
        json!({
            "capture_id": diagnostics.config.sensitive_trace_capture_id,
            "stop_reason": "user_requested",
            "effective_level": "WARN"
        }),
    );
    Ok(status())
}

pub(crate) fn application_log_directory() -> Result<PathBuf, String> {
    DIAGNOSTICS
        .get()
        .map(|diagnostics| diagnostics.config.log_directory.clone())
        .ok_or_else(|| "OpenKB application log location is unavailable.".to_owned())
}

pub(crate) fn sensitive_trace_directory() -> Result<PathBuf, String> {
    let diagnostics = DIAGNOSTICS
        .get()
        .ok_or_else(|| "OpenKB Sensitive Trace location is unavailable.".to_owned())?;
    let capture_id = diagnostics
        .config
        .sensitive_trace_capture_id
        .as_ref()
        .ok_or_else(|| "No Sensitive Trace Capture is configured.".to_owned())?;
    Ok(diagnostics.config.sensitive_trace_root.join(capture_id))
}

pub(crate) fn spawn_engine_stderr_reporter(mut stderr: ChildStderr) {
    thread::spawn(move || {
        let mut chunk = [0_u8; 4096];
        let mut line = Vec::with_capacity(512);
        let mut unclassified_seen = false;
        let mut unclassified_suppressed = 0_u64;
        loop {
            let Ok(read) = stderr.read(&mut chunk) else {
                return;
            };
            if read == 0 {
                if !line.is_empty() {
                    note_engine_stderr(&line, &mut unclassified_seen, &mut unclassified_suppressed);
                }
                if unclassified_suppressed > 0 {
                    event(
                        LogLevel::Warn,
                        "runtime",
                        "engine_stderr_suppressed",
                        "Repeated unclassified Engine stderr diagnostics were suppressed.",
                        json!({"suppressed_count": unclassified_suppressed}),
                    );
                }
                return;
            }
            for byte in &chunk[..read] {
                if *byte == b'\n' {
                    note_engine_stderr(&line, &mut unclassified_seen, &mut unclassified_suppressed);
                    line.clear();
                } else if line.len() < 4096 {
                    line.push(*byte);
                }
            }
        }
    });
}

fn note_engine_stderr(raw: &[u8], seen: &mut bool, suppressed: &mut u64) {
    if report_engine_stderr(raw, !*seen) {
        if *seen {
            *suppressed += 1;
        } else {
            *seen = true;
        }
    }
}

fn report_engine_stderr(raw: &[u8], report_unclassified: bool) -> bool {
    let text = String::from_utf8_lossy(raw);
    if text.starts_with("OPENKB_ENGINE_RUNTIME_FAILED failure_event_id=") {
        let failure_event_id = safe_identifier(text.split('=').nth(1).unwrap_or("unknown").trim());
        event(
            LogLevel::Error,
            "runtime",
            "engine_runtime_failure_reported",
            "The Engine reported a terminal runtime failure.",
            json!({"failure_event_id": failure_event_id}),
        );
        false
    } else if text.trim() == "OPENKB_LOGGING_UNAVAILABLE" {
        event(
            LogLevel::Warn,
            "runtime",
            "engine_logging_unavailable",
            "The Engine could not initialize its Application Log.",
            json!({"error_code": "engine_logging_unavailable"}),
        );
        false
    } else if !text.trim().is_empty() {
        if report_unclassified {
            event(
                LogLevel::Warn,
                "runtime",
                "engine_stderr_unclassified",
                "The Engine emitted an unclassified stderr diagnostic.",
                json!({"error_code": "engine_stderr_unclassified"}),
            );
        }
        true
    } else {
        false
    }
}

impl ShellLogWriter {
    fn note_drop(&self, level: LogLevel) {
        let counter = match level {
            LogLevel::Trace => &self.dropped_trace,
            LogLevel::Debug => &self.dropped_debug,
            _ => &self.dropped_info,
        };
        counter.fetch_add(1, Ordering::Relaxed);
    }

    fn write(&self, event: PendingEvent) {
        let Ok(_guard) = self.lock.lock() else { return };
        let dropped_trace = self.dropped_trace.swap(0, Ordering::AcqRel);
        let dropped_debug = self.dropped_debug.swap(0, Ordering::AcqRel);
        let dropped_info = self.dropped_info.swap(0, Ordering::AcqRel);
        if dropped_trace + dropped_debug + dropped_info > 0 {
            self.write_record(PendingEvent {
                level: LogLevel::Warn,
                component: "runtime".to_owned(),
                event: "diagnostic_events_dropped".to_owned(),
                summary: "Diagnostic events were dropped under pressure.".to_owned(),
                fields: sanitize_fields(json!({
                    "dropped_trace": dropped_trace,
                    "dropped_debug": dropped_debug,
                    "dropped_info": dropped_info,
                    "queue_capacity": EVENT_QUEUE_CAPACITY
                })),
            });
        }
        self.write_record(event);
    }

    fn write_record(&self, event: PendingEvent) {
        rotate_log(&self.path);
        let sequence = self.sequence.fetch_add(1, Ordering::AcqRel) + 1;
        let mut record = json!({
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_timestamp(SystemTime::now()),
            "level": event.level.as_str(),
            "event": event.event,
            "summary": event.summary,
            "runtime_session_id": self.runtime_session_id,
            "process": "shell",
            "pid": std::process::id(),
            "thread": thread::current().name().unwrap_or("unnamed"),
            "component": event.component,
            "sequence": sequence,
        });
        if let Some(record) = record.as_object_mut() {
            record.extend(event.fields);
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = serde_json::to_writer(&mut file, &record);
            let _ = file.write_all(b"\n");
            if event.level >= LogLevel::Warn {
                let _ = file.flush();
                let _ = file.sync_data();
            }
        }
    }
}

fn sanitize_fields(value: Value) -> Map<String, Value> {
    let Some(values) = value.as_object() else {
        return Map::new();
    };
    values
        .iter()
        .filter(|(key, _)| safe_field(key))
        .filter_map(|(key, value)| safe_value(value).map(|value| (key.clone(), value)))
        .collect()
}

fn safe_field(key: &str) -> bool {
    matches!(
        key,
        "attempt"
            | "capture_id"
            | "dropped_debug"
            | "dropped_info"
            | "dropped_trace"
            | "effective_level"
            | "elapsed_ms"
            | "error_code"
            | "error_type"
            | "failure_event_id"
            | "failure_kind"
            | "method"
            | "outcome"
            | "phase"
            | "queue_capacity"
            | "request_id"
            | "retryable"
            | "stage"
            | "status"
            | "stop_reason"
            | "suppressed_count"
            | "trace_components"
            | "warning_code"
    )
}

fn safe_value(value: &Value) -> Option<Value> {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => Some(value.clone()),
        Value::String(value) => Some(Value::String(value.chars().take(512).collect())),
        Value::Array(values) => Some(Value::Array(
            values.iter().filter_map(safe_value).take(64).collect(),
        )),
        _ => None,
    }
}

fn safe_event_name(value: &str) -> String {
    let valid = value.len() >= 2
        && value.len() <= 96
        && value.starts_with(|character: char| character.is_ascii_lowercase())
        && value.chars().all(|character| {
            character.is_ascii_lowercase()
                || character.is_ascii_digit()
                || "_.-".contains(character)
        });
    if valid {
        value.to_owned()
    } else {
        "invalid_diagnostic_event".to_owned()
    }
}

fn safe_identifier(value: &str) -> String {
    let valid = !value.is_empty()
        && value.len() <= 96
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_.-".contains(character));
    if valid {
        value.to_owned()
    } else {
        "invalid".to_owned()
    }
}

fn local_state_root(app: &AppHandle) -> Result<PathBuf, String> {
    if let Some(directory) = std::env::var_os("LOCALAPPDATA") {
        return Ok(PathBuf::from(directory).join("OpenKB"));
    }
    app.path()
        .app_data_dir()
        .map_err(|error| format!("OpenKB application state location is unavailable: {error}"))
}

fn rotate_log(path: &Path) {
    let Ok(metadata) = path.metadata() else {
        return;
    };
    if metadata.len() < MAX_LOG_BYTES {
        return;
    }
    let oldest = numbered_log(path, LOG_BACKUPS);
    let _ = fs::remove_file(oldest);
    for index in (1..LOG_BACKUPS).rev() {
        let _ = fs::rename(numbered_log(path, index), numbered_log(path, index + 1));
    }
    let _ = fs::rename(path, numbered_log(path, 1));
}

fn numbered_log(path: &Path, index: usize) -> PathBuf {
    PathBuf::from(format!("{}.{index}", path.to_string_lossy()))
}

fn migrate_legacy_logs(path: &Path) {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    for index in 0..=LOG_BACKUPS {
        let candidate = if index == 0 {
            path.to_path_buf()
        } else {
            numbered_log(path, index)
        };
        let structured = File::open(&candidate)
            .ok()
            .and_then(|file| BufReader::new(file).lines().next())
            .and_then(Result::ok)
            .and_then(|line| serde_json::from_str::<Value>(&line).ok())
            .is_some_and(|value| value["schema_version"] == SCHEMA_VERSION);
        if candidate.is_file() && !structured {
            let part = if index == 0 {
                String::new()
            } else {
                format!("-part{index}")
            };
            let mut destination =
                path.with_file_name(format!("openkb-shell.legacy-{timestamp}{part}.log"));
            let mut collision = 1;
            while destination.exists() {
                destination = path.with_file_name(format!(
                    "openkb-shell.legacy-{timestamp}{part}-{collision}.log"
                ));
                collision += 1;
            }
            let _ = fs::rename(candidate, destination);
        }
    }
}

fn directory_size(path: &Path) -> u64 {
    let Ok(entries) = fs::read_dir(path) else {
        return 0;
    };
    entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .map(|path| {
            if path.is_dir() {
                directory_size(&path)
            } else {
                path.metadata().map(|metadata| metadata.len()).unwrap_or(0)
            }
        })
        .sum()
}

fn utc_timestamp(now: SystemTime) -> String {
    let elapsed = now.duration_since(UNIX_EPOCH).unwrap_or_default();
    let days = (elapsed.as_secs() / 86_400) as i64;
    let seconds = elapsed.as_secs() % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        seconds / 3_600,
        (seconds % 3_600) / 60,
        seconds % 60,
        elapsed.subsec_millis()
    )
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(level: LogLevel) -> PendingEvent {
        PendingEvent {
            level,
            component: "runtime".to_owned(),
            event: "test_event".to_owned(),
            summary: "Test event.".to_owned(),
            fields: Map::new(),
        }
    }

    #[test]
    fn support_safe_fields_drop_unknown_payloads() {
        let fields = sanitize_fields(json!({
            "error_code": "provider_failure",
            "prompt": "secret",
            "raw_path": "private"
        }));
        assert_eq!(
            fields.get("error_code"),
            Some(&Value::from("provider_failure"))
        );
        assert!(!fields.contains_key("prompt"));
        assert!(!fields.contains_key("raw_path"));
    }

    #[test]
    fn utc_timestamp_has_millisecond_rfc3339_shape() {
        assert_eq!(utc_timestamp(UNIX_EPOCH), "1970-01-01T00:00:00.000Z");
        assert_eq!(safe_identifier("failure-123"), "failure-123");
        assert_eq!(safe_identifier("secret value"), "invalid");
        assert!(!report_engine_stderr(
            b"OPENKB_ENGINE_RUNTIME_FAILED failure_event_id=failure-123",
            true,
        ));
        assert!(report_engine_stderr(b"raw provider diagnostic", false));
    }

    #[test]
    fn bounded_queue_replaces_trace_before_debug() {
        let queue = BoundedEventQueue {
            capacity: 2,
            events: Mutex::new(VecDeque::new()),
            available: Condvar::new(),
        };
        assert_eq!(queue.push(pending(LogLevel::Trace)), None);
        assert_eq!(queue.push(pending(LogLevel::Trace)), None);
        assert_eq!(queue.push(pending(LogLevel::Debug)), Some(LogLevel::Trace));
        assert_eq!(queue.push(pending(LogLevel::Info)), Some(LogLevel::Trace));
        assert_eq!(queue.push(pending(LogLevel::Trace)), Some(LogLevel::Trace));
        assert_eq!(queue.pop().map(|event| event.level), Some(LogLevel::Debug));
        assert_eq!(queue.pop().map(|event| event.level), Some(LogLevel::Info));
    }
}
