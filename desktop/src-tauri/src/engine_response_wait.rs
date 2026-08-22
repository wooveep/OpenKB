//! Response waiting policy for Desktop Shell requests to the Python Engine.

use crate::engine_wire::BridgeResult;
use serde_json::Value;
use std::{sync::mpsc, time::Duration};

#[derive(Debug)]
pub(super) enum ResponseWaitError {
    Timeout,
    Disconnected,
}

pub(super) fn receive_response(
    receiver: mpsc::Receiver<BridgeResult<Value>>,
    timeout: Option<Duration>,
) -> Result<BridgeResult<Value>, ResponseWaitError> {
    match timeout {
        Some(timeout) => receiver.recv_timeout(timeout).map_err(|error| match error {
            mpsc::RecvTimeoutError::Timeout => ResponseWaitError::Timeout,
            mpsc::RecvTimeoutError::Disconnected => ResponseWaitError::Disconnected,
        }),
        None => receiver.recv().map_err(|_| ResponseWaitError::Disconnected),
    }
}

#[cfg(test)]
mod tests {
    use super::receive_response;
    use crate::engine_wire::BridgeResult;
    use serde_json::{json, Value};
    use std::{sync::mpsc, thread, time::Duration};

    #[test]
    fn unbounded_wait_accepts_a_later_explicit_response() {
        let (sender, receiver) = mpsc::channel::<BridgeResult<Value>>();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(25));
            sender.send(Ok(json!({"ok": true}))).unwrap();
        });

        let response = receive_response(receiver, None)
            .expect("channel should stay connected")
            .expect("Engine should return a result");

        assert_eq!(response, json!({"ok": true}));
    }
}
