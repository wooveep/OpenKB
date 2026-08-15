//! Engine restart and shutdown policy kept separate from request mediation.

use super::{mark_transport_failed, BridgeError, BridgeResult, EngineSupervisor, SHUTDOWN_TIMEOUT};
use serde_json::json;
use std::sync::atomic::Ordering;

impl EngineSupervisor {
    /// Remember the current workbench so one automatic child restart can reopen it.
    pub fn remember_active_knowledge_base(&self, kb_dir: String) {
        if let Ok(mut active_kb_dir) = self.active_kb_dir.lock() {
            *active_kb_dir = Some(kb_dir);
        }
    }

    /// Restart one unexpectedly exited child while the Desktop Runtime remains open.
    pub fn restart_after_unexpected_exit(&self) -> bool {
        if !self.has_started.load(Ordering::Acquire)
            || self.stopping.load(Ordering::Acquire)
            || self.transport.connected.load(Ordering::Acquire)
            || self.restart_attempted.swap(true, Ordering::AcqRel)
        {
            return false;
        }
        match self.ensure_started_inner(true) {
            Ok(_) => {
                eprintln!("OpenKB Desktop Engine restarted after an unexpected exit.");
                true
            }
            Err(error) => {
                eprintln!("OpenKB Desktop Engine restart failed: {}", error.message);
                false
            }
        }
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

    pub(super) fn restore_active_knowledge_base(&self) -> BridgeResult<()> {
        let active_kb_dir = self
            .active_kb_dir
            .lock()
            .map_err(|_| {
                BridgeError::new(
                    "engine_unavailable",
                    "Desktop active knowledge-base state is unavailable.",
                )
            })?
            .clone();
        if let Some(kb_dir) = active_kb_dir {
            self.request_started(
                "workbench.open_knowledge_base",
                json!({ "kb_dir": kb_dir }),
                None,
            )?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::EngineSupervisor;
    use std::sync::atomic::Ordering;

    #[test]
    fn shutdown_refuses_to_start_a_new_engine() {
        let supervisor = EngineSupervisor::default();
        supervisor.shutdown();

        let error = supervisor
            .handshake()
            .expect_err("shutdown must be terminal");
        assert_eq!(error.code, "engine_stopping");
    }

    #[test]
    fn request_path_waits_for_the_single_automatic_recovery_after_engine_exit() {
        let supervisor = EngineSupervisor::default();
        supervisor.has_started.store(true, Ordering::Release);

        let error = supervisor
            .handshake()
            .expect_err("requests must not create another automatic restart");
        assert_eq!(error.code, "engine_unavailable");
    }
}
