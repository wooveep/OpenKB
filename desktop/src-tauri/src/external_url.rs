//! Small system-browser launcher for safe links rendered inside Desktop answers.

use crate::engine_protocol::BridgeError;
use std::process::Command;

pub fn open_in_system_browser(url: &str) -> Result<(), BridgeError> {
    if url.len() > 2048
        || url.chars().any(char::is_control)
        || !(url.starts_with("https://") || url.starts_with("http://"))
    {
        return Err(BridgeError::new(
            "desktop_external_url_invalid",
            "Only HTTP and HTTPS links can be opened.",
        ));
    }

    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("rundll32.exe");
        command.args(["url.dll,FileProtocolHandler", url]);
        command
    };
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("open");
        command.arg(url);
        command
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut command = Command::new("xdg-open");
        command.arg(url);
        command
    };

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn().map(|_| ()).map_err(|error| {
        BridgeError::new(
            "desktop_external_url_open_failed",
            format!("Could not open the system browser: {error}"),
        )
    })
}
