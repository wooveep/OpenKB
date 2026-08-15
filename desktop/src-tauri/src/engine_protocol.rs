//! Private Desktop Shell ↔ Python Engine JSON-RPC transport.
//!
//! The protocol is deliberately local to the child-process stdio pair. The
//! React workbench reaches it only through typed Tauri commands and channels.

use crate::engine_wire::{
    parse_response, read_frame, write_frame, EngineHandshake, EngineHealthWire,
};
pub use crate::engine_wire::{
    ActiveKnowledgeBaseResult, BridgeError, BridgeEvent, BridgeHandshake, BridgeResult,
    CancelResult, DocumentVersionCandidate, DocumentVersionCandidateDecision,
    DocumentVersionCandidatesResult, EngineHealth, GroundedAnswer, GroundedAnswersResult,
    ImportControlResult, ImportJobsResult, ImportSourceInspection, InspectKnowledgeBaseResult,
    KnowledgeBaseActivationResult, KnowledgePage, KnowledgePageKind, KnowledgePagesResult,
    KnowledgeReconciliationCommit, KnowledgeReconciliationConflict,
    KnowledgeReconciliationConflictsResult, KnowledgeReconciliationDecision, RawDocument,
    RecoveryOverride, TextDocumentImportResult,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    env,
    io::Read,
    process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::Duration,
};
use tauri::ipc::Channel;

#[path = "engine_protocol_answers.rs"]
mod answers;
#[path = "engine_protocol_document_versions.rs"]
mod document_versions;
#[path = "engine_protocol_knowledge_pages.rs"]
mod knowledge_pages;
#[path = "engine_protocol_knowledge_reconciliation.rs"]
mod knowledge_reconciliation;

const PROTOCOL_VERSION: u32 = 1;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
const IMPORT_REQUEST_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);

struct SharedTransport {
    writer: Mutex<Option<ChildStdin>>,
    pending: Mutex<HashMap<String, mpsc::Sender<BridgeResult<Value>>>>,
    subscribers: Mutex<HashMap<String, Channel<BridgeEvent>>>,
    connected: AtomicBool,
}

impl Default for SharedTransport {
    fn default() -> Self {
        Self {
            writer: Mutex::new(None),
            pending: Mutex::new(HashMap::new()),
            subscribers: Mutex::new(HashMap::new()),
            connected: AtomicBool::new(false),
        }
    }
}

/// Supervises one persistent Python Engine child for the Desktop Runtime.
pub struct EngineSupervisor {
    transport: Arc<SharedTransport>,
    child: Mutex<Option<Child>>,
    handshake: Mutex<Option<BridgeHandshake>>,
    start_guard: Mutex<()>,
    stopping: AtomicBool,
    request_sequence: AtomicU64,
}

impl Default for EngineSupervisor {
    fn default() -> Self {
        Self {
            transport: Arc::new(SharedTransport::default()),
            child: Mutex::new(None),
            handshake: Mutex::new(None),
            start_guard: Mutex::new(()),
            stopping: AtomicBool::new(false),
            request_sequence: AtomicU64::new(0),
        }
    }
}

impl EngineSupervisor {
    /// Start the Engine if needed and verify the versioned Bridge handshake.
    pub fn start(&self) -> BridgeResult<BridgeHandshake> {
        self.ensure_started()
    }

    pub fn handshake(&self) -> BridgeResult<BridgeHandshake> {
        self.ensure_started()
    }

    pub fn health(&self) -> BridgeResult<EngineHealth> {
        self.ensure_started()?;
        let value = self.request_started("engine.health", json!({}), None)?;
        let wire: EngineHealthWire = serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine health response has an invalid shape: {error}"),
            )
        })?;
        Ok(EngineHealth {
            status: wire.status,
            protocol_version: wire.protocol_version,
        })
    }

    pub fn inspect_knowledge_base(
        &self,
        kb_dir: String,
        request_id: String,
    ) -> BridgeResult<InspectKnowledgeBaseResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.inspect_knowledge_base",
            json!({ "kb_dir": kb_dir }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine inspection response has an invalid shape: {error}"),
            )
        })
    }

    pub fn create_knowledge_base(
        &self,
        kb_dir: String,
        name: Option<String>,
        request_id: String,
    ) -> BridgeResult<KnowledgeBaseActivationResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.create_knowledge_base",
            json!({ "kb_dir": kb_dir, "name": name }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-base creation response has an invalid shape: {error}"),
            )
        })
    }

    pub fn open_knowledge_base(
        &self,
        kb_dir: String,
        request_id: String,
    ) -> BridgeResult<KnowledgeBaseActivationResult> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.open_knowledge_base",
            json!({ "kb_dir": kb_dir }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine knowledge-base open response has an invalid shape: {error}"),
            )
        })
    }

    pub fn active_knowledge_base(&self) -> BridgeResult<ActiveKnowledgeBaseResult> {
        self.ensure_started()?;
        let value = self.request_started("workbench.active_knowledge_base", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine active knowledge-base response has an invalid shape: {error}"),
            )
        })
    }

    pub fn inspect_import_sources(
        &self,
        source_paths: Vec<String>,
        request_id: String,
    ) -> BridgeResult<ImportSourceInspection> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.inspect_import_sources",
            json!({ "source_paths": source_paths }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine import source inspection response has an invalid shape: {error}"),
            )
        })
    }

    pub fn import_text_document(
        &self,
        source_path: String,
        request_id: String,
    ) -> BridgeResult<TextDocumentImportResult> {
        self.ensure_started()?;
        let value = self.request_started_with_timeout(
            "workbench.import_text_document",
            json!({ "source_path": source_path }),
            Some(request_id),
            IMPORT_REQUEST_TIMEOUT,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine document import response has an invalid shape: {error}"),
            )
        })
    }

    pub fn import_jobs(&self) -> BridgeResult<ImportJobsResult> {
        self.ensure_started()?;
        let value = self.request_started("workbench.import_jobs", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine import jobs response has an invalid shape: {error}"),
            )
        })
    }

    pub fn grounded_answers(&self) -> BridgeResult<GroundedAnswersResult> {
        self.ensure_started()?;
        let value = self.request_started("workbench.grounded_answers", json!({}), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine grounded answer history has an invalid shape: {error}"),
            )
        })
    }

    pub fn read_raw_document(
        &self,
        document_id: String,
        request_id: String,
        page: u32,
        focus_locator: Option<serde_json::Value>,
    ) -> BridgeResult<RawDocument> {
        self.ensure_started()?;
        let value = self.request_started(
            "workbench.read_raw_document",
            json!({
                "document_id": document_id,
                "page": page,
                "focus_locator": focus_locator,
            }),
            Some(request_id),
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine raw document response has an invalid shape: {error}"),
            )
        })
    }

    pub fn pause_import_job(&self, job_id: String) -> BridgeResult<ImportControlResult> {
        self.import_control("workbench.pause_import_job", job_id)
    }

    pub fn resume_import_job(
        &self,
        job_id: String,
        request_id: String,
    ) -> BridgeResult<TextDocumentImportResult> {
        self.ensure_started()?;
        let value = self.request_started_with_timeout(
            "workbench.resume_import_job",
            json!({ "job_id": job_id }),
            Some(request_id),
            IMPORT_REQUEST_TIMEOUT,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine import resume response has an invalid shape: {error}"),
            )
        })
    }

    pub fn recover_import_job(
        &self,
        job_id: String,
        recovery_override: RecoveryOverride,
        request_id: String,
    ) -> BridgeResult<TextDocumentImportResult> {
        self.ensure_started()?;
        let value = self.request_started_with_timeout(
            "workbench.recover_import_job",
            json!({ "job_id": job_id, "recovery_override": recovery_override }),
            Some(request_id),
            IMPORT_REQUEST_TIMEOUT,
        )?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine import recovery response has an invalid shape: {error}"),
            )
        })
    }

    pub fn cancel_import_job(&self, job_id: String) -> BridgeResult<ImportControlResult> {
        self.import_control("workbench.cancel_import_job", job_id)
    }

    pub fn cancel(&self, request_id: String) -> BridgeResult<CancelResult> {
        self.ensure_started()?;
        let value =
            self.request_started("engine.cancel", json!({ "request_id": request_id }), None)?;
        let cancelled = value
            .get("cancelled")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                BridgeError::new(
                    "invalid_engine_response",
                    "Engine cancel response is invalid.",
                )
            })?;
        let returned_request_id = value
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| {
                BridgeError::new(
                    "invalid_engine_response",
                    "Engine cancel response is invalid.",
                )
            })?;
        Ok(CancelResult {
            cancelled,
            request_id: returned_request_id,
        })
    }

    fn import_control(&self, method: &str, job_id: String) -> BridgeResult<ImportControlResult> {
        self.ensure_started()?;
        let value = self.request_started(method, json!({ "job_id": job_id }), None)?;
        serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine import control response has an invalid shape: {error}"),
            )
        })
    }

    /// Ask the owned Engine to stop, then reap it so the Shell never leaves work behind.
    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        let _startup_guard = match self.start_guard.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        if self.transport.connected.load(Ordering::Acquire) {
            let _ = self.request_started_with_timeout(
                "engine.shutdown",
                json!({}),
                None,
                SHUTDOWN_TIMEOUT,
            );
        }
        mark_transport_failed(
            &self.transport,
            BridgeError::new(
                "engine_shutdown",
                "Desktop Shell is shutting down the Python Engine.",
            ),
        );
        if let Ok(mut handshake) = self.handshake.lock() {
            *handshake = None;
        }
        let _ = self.stop_child_with_grace(SHUTDOWN_TIMEOUT);
    }

    pub fn subscribe(
        &self,
        subscription_id: String,
        channel: Channel<BridgeEvent>,
    ) -> BridgeResult<()> {
        self.ensure_started()?;
        let mut subscribers = self.transport.subscribers.lock().map_err(|_| {
            BridgeError::new(
                "engine_unavailable",
                "Engine subscriber state is unavailable.",
            )
        })?;
        subscribers.insert(subscription_id, channel);
        Ok(())
    }

    pub fn unsubscribe(&self, subscription_id: &str) {
        if let Ok(mut subscribers) = self.transport.subscribers.lock() {
            subscribers.remove(subscription_id);
        }
    }

    fn ensure_started(&self) -> BridgeResult<BridgeHandshake> {
        self.require_running()?;
        if let Some(handshake) = self.current_handshake()? {
            return Ok(handshake);
        }

        let _guard = self.start_guard.lock().map_err(|_| {
            BridgeError::new("engine_unavailable", "Engine startup state is unavailable.")
        })?;
        self.require_running()?;
        if let Some(handshake) = self.current_handshake()? {
            return Ok(handshake);
        }

        self.reap_stale_child()?;
        self.spawn_engine()?;
        let value = self.request_started(
            "engine.handshake",
            json!({ "protocol_version": PROTOCOL_VERSION }),
            None,
        )?;
        let wire: EngineHandshake = serde_json::from_value(value).map_err(|error| {
            BridgeError::new(
                "invalid_engine_response",
                format!("Engine handshake response has an invalid shape: {error}"),
            )
        })?;
        if wire.protocol_version != PROTOCOL_VERSION {
            return Err(BridgeError::new(
                "protocol_version_incompatible",
                format!(
                    "Desktop Shell requires protocol version {PROTOCOL_VERSION}, but Engine reported {}.",
                    wire.protocol_version
                ),
            ));
        }

        let handshake = BridgeHandshake {
            protocol_version: wire.protocol_version,
            engine_version: wire.engine_version,
        };
        let mut stored = self.handshake.lock().map_err(|_| {
            BridgeError::new(
                "engine_unavailable",
                "Engine handshake state is unavailable.",
            )
        })?;
        *stored = Some(handshake.clone());
        Ok(handshake)
    }

    fn current_handshake(&self) -> BridgeResult<Option<BridgeHandshake>> {
        if !self.transport.connected.load(Ordering::Acquire) {
            let mut handshake = self.handshake.lock().map_err(|_| {
                BridgeError::new(
                    "engine_unavailable",
                    "Engine handshake state is unavailable.",
                )
            })?;
            *handshake = None;
            return Ok(None);
        }
        self.handshake
            .lock()
            .map(|handshake| handshake.clone())
            .map_err(|_| {
                BridgeError::new(
                    "engine_unavailable",
                    "Engine handshake state is unavailable.",
                )
            })
    }

    fn require_running(&self) -> BridgeResult<()> {
        if self.stopping.load(Ordering::Acquire) {
            return Err(BridgeError::new(
                "engine_stopping",
                "Desktop Shell is shutting down the Python Engine.",
            ));
        }
        Ok(())
    }

    fn spawn_engine(&self) -> BridgeResult<()> {
        let mut command = engine_command();
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("PYTHONUNBUFFERED", "1");
        let mut child = command.spawn().map_err(|error| {
            BridgeError::new(
                "engine_start_failed",
                format!("Could not start Python Engine: {error}"),
            )
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            BridgeError::new("engine_start_failed", "Engine stdin is unavailable.")
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            BridgeError::new("engine_start_failed", "Engine stdout is unavailable.")
        })?;
        let stderr = child.stderr.take().ok_or_else(|| {
            BridgeError::new("engine_start_failed", "Engine stderr is unavailable.")
        })?;

        {
            let mut writer = self.transport.writer.lock().map_err(|_| {
                BridgeError::new("engine_unavailable", "Engine writer state is unavailable.")
            })?;
            *writer = Some(stdin);
        }
        self.transport.connected.store(true, Ordering::Release);
        {
            let mut managed_child = self.child.lock().map_err(|_| {
                BridgeError::new("engine_unavailable", "Engine process state is unavailable.")
            })?;
            *managed_child = Some(child);
        }
        spawn_stdout_reader(Arc::clone(&self.transport), stdout);
        spawn_stderr_reporter(stderr);
        Ok(())
    }

    fn reap_stale_child(&self) -> BridgeResult<()> {
        self.stop_child_with_grace(Duration::from_secs(0))
    }

    fn stop_child_with_grace(&self, grace_period: Duration) -> BridgeResult<()> {
        let stale_child = self
            .child
            .lock()
            .map_err(|_| {
                BridgeError::new("engine_unavailable", "Engine process state is unavailable.")
            })?
            .take();
        if let Some(mut child) = stale_child {
            let deadline = std::time::Instant::now() + grace_period;
            loop {
                if child
                    .try_wait()
                    .map_err(|error| {
                        BridgeError::new(
                            "engine_unavailable",
                            format!("Could not inspect Python Engine state: {error}"),
                        )
                    })?
                    .is_some()
                {
                    return Ok(());
                }
                if std::time::Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Ok(());
                }
                thread::sleep(Duration::from_millis(25));
            }
        }
        Ok(())
    }

    fn request_started(
        &self,
        method: &str,
        params: Value,
        caller_request_id: Option<String>,
    ) -> BridgeResult<Value> {
        self.request_started_with_timeout(method, params, caller_request_id, REQUEST_TIMEOUT)
    }

    fn request_started_with_timeout(
        &self,
        method: &str,
        params: Value,
        caller_request_id: Option<String>,
        timeout: Duration,
    ) -> BridgeResult<Value> {
        let request_id = caller_request_id.unwrap_or_else(|| self.next_request_id());
        if request_id.trim().is_empty() {
            return Err(BridgeError::new(
                "invalid_request_id",
                "Desktop Bridge request id must not be empty.",
            ));
        }
        let (sender, receiver) = mpsc::channel();
        {
            let mut pending = self.transport.pending.lock().map_err(|_| {
                BridgeError::new("engine_unavailable", "Engine request state is unavailable.")
            })?;
            if pending.contains_key(&request_id) {
                return Err(BridgeError::new(
                    "duplicate_request_id",
                    format!("A Desktop Bridge request is already running with id {request_id:?}."),
                ));
            }
            pending.insert(request_id.clone(), sender);
        }

        let request = json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        });
        let write_result = {
            let mut writer = self.transport.writer.lock().map_err(|_| {
                BridgeError::new("engine_unavailable", "Engine writer state is unavailable.")
            })?;
            let writer = writer.as_mut().ok_or_else(|| {
                BridgeError::new("engine_unavailable", "Python Engine is not running.")
            })?;
            write_frame(writer, &request)
        };
        if let Err(error) = write_result {
            mark_transport_failed(&self.transport, error.clone());
            return Err(error);
        }

        match receiver.recv_timeout(timeout) {
            Ok(result) => result,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if let Ok(mut pending) = self.transport.pending.lock() {
                    pending.remove(&request_id);
                }
                self.best_effort_cancel(&request_id, method);
                Err(BridgeError::new(
                    "engine_request_timeout",
                    format!("Python Engine did not answer {method} in time."),
                ))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(BridgeError::new(
                "engine_unavailable",
                "Python Engine stopped before responding.",
            )),
        }
    }

    fn next_request_id(&self) -> String {
        let sequence = self.request_sequence.fetch_add(1, Ordering::Relaxed) + 1;
        format!("desktop-{sequence}")
    }

    fn best_effort_cancel(&self, target_request_id: &str, timed_out_method: &str) {
        if matches!(timed_out_method, "engine.cancel" | "engine.shutdown") {
            return;
        }
        let request = json!({
            "jsonrpc": "2.0",
            "id": self.next_request_id(),
            "method": "engine.cancel",
            "params": { "request_id": target_request_id },
        });
        let write_result = self
            .transport
            .writer
            .lock()
            .ok()
            .and_then(|mut writer| writer.as_mut().map(|writer| write_frame(writer, &request)));
        if let Some(Err(error)) = write_result {
            mark_transport_failed(&self.transport, error);
        }
    }
}

fn engine_command() -> Command {
    if let Ok(engine_path) = env::var("OPENKB_ENGINE_PATH") {
        return Command::new(engine_path);
    }

    if let Ok(shell_path) = env::current_exe() {
        if let Some(shell_dir) = shell_path.parent() {
            let packaged_engine = shell_dir
                .join("runtime")
                .join("engine")
                .join("OpenKBEngine.exe");
            if packaged_engine.is_file() {
                return Command::new(packaged_engine);
            }
        }
    }

    let mut command = Command::new("uv");
    command
        .arg("run")
        .arg("openkb-desktop-engine")
        .current_dir(env!("CARGO_MANIFEST_DIR"));
    command
}

fn spawn_stdout_reader(transport: Arc<SharedTransport>, mut stdout: ChildStdout) {
    thread::spawn(move || loop {
        match read_frame(&mut stdout) {
            Ok(Some(message)) => route_message(&transport, message),
            Ok(None) => {
                mark_transport_failed(
                    &transport,
                    BridgeError::new(
                        "engine_unavailable",
                        "Python Engine closed its protocol stream.",
                    ),
                );
                return;
            }
            Err(error) => {
                mark_transport_failed(&transport, error);
                return;
            }
        }
    });
}

fn spawn_stderr_reporter(mut stderr: ChildStderr) {
    thread::spawn(move || {
        let mut text = String::new();
        if stderr.read_to_string(&mut text).is_ok() && !text.trim().is_empty() {
            eprintln!("OpenKB Python Engine: {}", text.trim());
        }
    });
}

fn route_message(transport: &SharedTransport, message: Value) {
    if let Some(request_id) = message.get("id").and_then(Value::as_str) {
        let sender = transport
            .pending
            .lock()
            .ok()
            .and_then(|mut pending| pending.remove(request_id));
        if let Some(sender) = sender {
            let _ = sender.send(parse_response(message));
        }
        return;
    }

    if message.get("method").and_then(Value::as_str) == Some("event") {
        let Some(params) = message.get("params") else {
            return;
        };
        let Ok(event) = serde_json::from_value::<BridgeEvent>(params.clone()) else {
            return;
        };
        if let Ok(mut subscribers) = transport.subscribers.lock() {
            subscribers.retain(|_, channel| channel.send(event.clone()).is_ok());
        }
    }
}

fn fail_pending(transport: &SharedTransport, error: BridgeError) {
    let pending = transport
        .pending
        .lock()
        .map(|mut pending| {
            pending
                .drain()
                .map(|(_, sender)| sender)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    for sender in pending {
        let _ = sender.send(Err(error.clone()));
    }
}

fn mark_transport_failed(transport: &SharedTransport, error: BridgeError) {
    transport.connected.store(false, Ordering::Release);
    if let Ok(mut writer) = transport.writer.lock() {
        writer.take();
    }
    fail_pending(transport, error);
}

#[cfg(test)]
mod tests {
    use super::EngineSupervisor;

    #[test]
    fn shutdown_refuses_to_start_a_new_engine() {
        let supervisor = EngineSupervisor::default();
        supervisor.shutdown();

        let error = supervisor
            .handshake()
            .expect_err("shutdown must be terminal");
        assert_eq!(error.code, "engine_stopping");
    }
}
