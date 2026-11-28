// desktop.json beside the engine's own files: the app's settings, never read by the engine.
use serde::{Deserialize, Serialize};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

pub const FILE: &str = "desktop.json";
/// `calsync web` and the Docker image use 8484, and WSL's localhost forwarding makes a CalSync in WSL
/// answer on Windows' 127.0.0.1:8484 too. The app never takes it.
pub const SHARED_PORT: u16 = 8484;
/// The app's own port, and the range it tries when that one is taken.
pub const FIRST_PORT: u16 = 8495;
const LAST_PORT: u16 = 8520;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Settings {
    /// Closing the window hides it to the tray and sync carries on. Off: closing quits.
    pub keep_background: bool,
    /// Pause from the tray: the scheduler skips its runs. Sync now still works.
    pub paused: bool,
    /// The port last used, kept so the page's address (and its sign-in cookie) stays the same.
    pub port: Option<u16>,
    /// The "still running in the tray" notice is shown once.
    pub told_about_tray: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { keep_background: true, paused: false, port: None, told_about_tray: false }
    }
}

pub fn path(dir: &Path) -> PathBuf {
    dir.join(FILE)
}

pub fn load(dir: &Path) -> Settings {
    std::fs::read_to_string(path(dir)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

pub fn save(dir: &Path, s: &Settings) {
    let tmp = dir.join(format!("{FILE}.tmp"));
    let body = serde_json::to_string_pretty(s).unwrap_or_default();
    if let Err(e) = std::fs::write(&tmp, body).and_then(|_| std::fs::rename(&tmp, path(dir))) {
        log::warn!("{FILE}: {e}");
    }
}

fn free(port: u16) -> bool {
    port != SHARED_PORT && TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// The saved port if it's still free, else the first free one from 8495, else any the system gives.
pub fn pick_port(saved: Option<u16>) -> u16 {
    if let Some(p) = saved.filter(|p| free(*p)) {
        return p;
    }
    if let Some(p) = (FIRST_PORT..=LAST_PORT).find(|p| free(*p)) {
        return p;
    }
    TcpListener::bind(("127.0.0.1", 0)).and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(FIRST_PORT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn never_the_shared_port() {
        assert!(!free(SHARED_PORT));
        assert_ne!(pick_port(Some(SHARED_PORT)), SHARED_PORT);
    }

    #[test]
    fn a_taken_port_is_skipped() {
        let held = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let p = held.local_addr().unwrap().port();
        assert_ne!(pick_port(Some(p)), p);
    }

    #[test]
    fn old_or_partial_files_still_load() {
        let s: Settings = serde_json::from_str(r#"{"paused":true}"#).unwrap();
        assert!(s.paused && s.keep_background && s.port.is_none());
    }
}
