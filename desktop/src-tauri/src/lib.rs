// CalSync's desktop app: runs the engine (engine.rs) on 127.0.0.1, shows its setup page in an app
// window, and keeps a tray icon (tray.rs). Settings: "Keep syncing in the background" (closing the
// window hides it and sync carries on) and "Start on login" (starts hidden, in the tray).
// Data: desktop.json and the engine's files (data\) in %LOCALAPPDATA%\app.getcalsync.desktop.
mod engine;
mod settings;
mod tray;

use settings::Settings;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, RunEvent, Runtime, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_opener::OpenerExt as _;
use url::Url;

pub const MAIN: &str = "main";
/// Start on login passes this, so the app starts in the tray without a window.
pub const BACKGROUND_ARG: &str = "--background";

pub struct Desk {
    /// desktop.json, and the engine's CALSYNC_HOME in data\.
    pub dir: PathBuf,
    pub port: u16,
    pub settings: Mutex<Settings>,
    last_problems: Mutex<u32>,
}

pub fn desk<R: Runtime>(app: &AppHandle<R>) -> tauri::State<'_, Desk> {
    app.state::<Desk>()
}

/// The app's own data folder: %LOCALAPPDATA%\app.getcalsync.desktop on Windows,
/// ~/Library/Application Support/app.getcalsync.desktop on a Mac. Local, not roaming: it holds
/// sign-in tokens, which shouldn't follow a profile around. Not %LOCALAPPDATA%\CalSync, which is
/// where the per-user installer puts the program. desktop.json is here; the engine's files are in data\.
fn data_dir<R: Runtime>(app: &AppHandle<R>) -> PathBuf {
    if let Some(dir) = std::env::var_os("CALSYNC_DESKTOP_HOME") {
        return PathBuf::from(dir);
    }
    app.path().app_local_data_dir().unwrap_or_else(|_| std::env::temp_dir().join("app.getcalsync.desktop"))
}

fn page_url(port: u16) -> Url {
    Url::parse(&format!("http://127.0.0.1:{port}/")).expect("a loopback address")
}

/// The app's own pages (the loading screen) and the engine's page stay in the window; everything
/// else, like Google's and Microsoft's sign-in, opens in the system browser.
fn is_inside(url: &Url, port: u16) -> bool {
    match url.scheme() {
        "tauri" => true,
        "http" | "https" if url.host_str() == Some("tauri.localhost") => true,
        "http" => url.host_str() == Some("127.0.0.1") && url.port() == Some(port),
        _ => false,
    }
}

fn open_outside<R: Runtime>(app: &AppHandle<R>, url: &Url) {
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        if let Err(e) = app.opener().open_url(url.as_str(), None::<&str>) {
            log::warn!("open in browser: {e}");
        }
    }
}

fn create_main<R: Runtime>(app: &AppHandle<R>, visible: bool) -> tauri::Result<()> {
    let port = desk(app).port;
    let (nav, new) = (app.clone(), app.clone());
    WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
        .title("CalSync")
        .inner_size(1120.0, 800.0)
        .min_inner_size(380.0, 520.0)
        .center()
        .visible(visible)
        .background_color(tauri::window::Color(255, 255, 255, 255))
        .on_navigation(move |url| {
            let inside = is_inside(url, port);
            if !inside {
                open_outside(&nav, url);
            }
            inside
        })
        .on_new_window(move |url, _features| {
            if is_inside(&url, port) {
                if let Some(w) = new.get_webview_window(MAIN) {
                    let _ = w.navigate(url);
                }
            } else {
                open_outside(&new, &url);
            }
            NewWindowResponse::Deny
        })
        .build()?;
    Ok(())
}

pub fn show_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// The engine answers: the window leaves the loading screen for the page.
pub fn page_ready<R: Runtime>(app: &AppHandle<R>) {
    let port = desk(app).port;
    if let Some(w) = app.get_webview_window(MAIN) {
        let on_page = w.url().is_ok_and(|u| is_inside(&u, port) && u.scheme() == "http" && u.host_str() == Some("127.0.0.1"));
        if !on_page {
            let _ = w.navigate(page_url(port));
        }
    }
    tray::refresh(app);
}

pub fn engine_failed<R: Runtime>(app: &AppHandle<R>, msg: &str) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let mut u = Url::parse("http://tauri.localhost/index.html").unwrap();
        if let Ok(cur) = w.url() {
            if cur.scheme() == "tauri" {
                u = Url::parse("tauri://localhost/index.html").unwrap();
            }
        }
        u.query_pairs_mut().append_pair("error", msg);
        let _ = w.navigate(u);
    }
    tray::refresh(app);
}

pub fn on_status<R: Runtime>(app: &AppHandle<R>, s: &engine::Status) {
    tray::refresh(app);
    // A notice when a sync that was fine starts having problems, not on every run that still has them.
    let d = desk(app);
    let n = s.problems.max(s.errors);
    let before = std::mem::replace(&mut *d.last_problems.lock().unwrap(), n);
    if n > 0 && before == 0 && !s.running {
        let body = s.problem.clone().unwrap_or_else(|| "Open CalSync to see what went wrong.".into());
        notice(app, "CalSync couldn't sync everything", &body);
    }
}

fn notice<R: Runtime>(app: &AppHandle<R>, title: &str, body: &str) {
    let mut n = notify_rust::Notification::new();
    n.summary(title).body(body).auto_icon();
    #[cfg(windows)]
    {
        // The installer's Start menu shortcut carries this id; a target/ build has none and keeps the default.
        let installed = tauri::utils::platform::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_string_lossy().replace('\\', "/")))
            .is_some_and(|d| !d.contains("/target/"));
        if installed {
            n.app_id(&app.config().identifier);
        }
    }
    let _ = app;
    std::thread::spawn(move || {
        if let Err(e) = n.show() {
            log::warn!("notice: {e}");
        }
    });
}

fn save_settings<R: Runtime>(app: &AppHandle<R>, change: impl FnOnce(&mut Settings)) -> Settings {
    let d = desk(app);
    let mut s = d.settings.lock().unwrap();
    change(&mut s);
    settings::save(&d.dir, &s);
    s.clone()
}

pub fn toggle_pause<R: Runtime>(app: &AppHandle<R>) {
    let s = save_settings(app, |s| s.paused = !s.paused);
    engine::send(app, if s.paused { "pause" } else { "resume" });
}

pub fn toggle_background<R: Runtime>(app: &AppHandle<R>) {
    let s = save_settings(app, |s| s.keep_background = !s.keep_background);
    if let Some(items) = tray::items(app) {
        let _ = items.background.set_checked(s.keep_background);
    }
}

pub fn toggle_login<R: Runtime>(app: &AppHandle<R>) {
    let a = app.autolaunch();
    let want = !a.is_enabled().unwrap_or(false);
    if let Err(e) = if want { a.enable() } else { a.disable() } {
        log::warn!("start on login: {e}");
    }
    if let Some(items) = tray::items(app) {
        let _ = items.login.set_checked(a.is_enabled().unwrap_or(false));
    }
}

/// `CalSync.exe --tray sync` (or open, pause, background, login, quit) does what that tray item
/// does, in the running app. For scripts and for testing the tray without clicking it.
const TRAY_ACTIONS: [&str; 6] = ["open", "sync", "pause", "background", "login", "quit"];

fn tray_action(argv: &[String]) -> Option<&str> {
    let i = argv.iter().position(|a| a == "--tray")?;
    argv.get(i + 1).map(String::as_str).filter(|a| TRAY_ACTIONS.contains(a))
}

fn tray_command<R: Runtime>(app: &AppHandle<R>, action: &str) {
    log::info!("tray command: {action}");
    match action {
        "open" => show_main(app),
        "sync" => engine::send(app, "sync"),
        "pause" => toggle_pause(app),
        "background" => toggle_background(app),
        "login" => toggle_login(app),
        "quit" => quit(app),
        _ => {}
    }
}

pub fn quit<R: Runtime>(app: &AppHandle<R>) {
    engine::stop(app);
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let background = std::env::args().any(|a| a == BACKGROUND_ARG);
    let app = tauri::Builder::default()
        // First, so opening CalSync again brings up this one's window instead of a second engine.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| match tray_action(&argv) {
            Some(action) => tray_command(app, action),
            None if argv.iter().any(|a| a == BACKGROUND_ARG) => {}
            None => show_main(app),
        }))
        .plugin(
            tauri_plugin_log::Builder::new()
                .clear_targets()
                .level(log::LevelFilter::Info)
                .target(tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::LogDir { file_name: Some("calsync".into()) }))
                .target(tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stdout))
                .max_file_size(1_000_000)
                .build(),
        )
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec![BACKGROUND_ARG])))
        .manage(engine::Engine::default())
        .setup(move |app| {
            let handle = app.handle().clone();
            let dir = data_dir(&handle);
            std::fs::create_dir_all(&dir)?;
            let s = settings::load(&dir);
            let port = settings::pick_port(s.port);
            if s.port != Some(port) {
                let mut saved = s.clone();
                saved.port = Some(port);
                settings::save(&dir, &saved);
            }
            log::info!("CalSync {} data in {}, page on 127.0.0.1:{port}", env!("CARGO_PKG_VERSION"), dir.display());
            let keep = s.keep_background;
            app.manage(Desk { dir, port, settings: Mutex::new(Settings { port: Some(port), ..s }), last_problems: Mutex::new(0) });
            let login = handle.autolaunch().is_enabled().unwrap_or(false);
            tray::create(&handle, keep, login)?;
            create_main(&handle, !background)?;
            engine::start(&handle);
            // "3 min ago" moves on even when nothing else changes.
            let tick = handle.clone();
            std::thread::spawn(move || loop {
                std::thread::sleep(std::time::Duration::from_secs(30));
                tray::refresh(&tick);
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != MAIN {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                if desk(app).settings.lock().unwrap().keep_background {
                    api.prevent_close();
                    let _ = window.hide();
                    let first = !save_settings(app, |_| {}).told_about_tray;
                    if first {
                        save_settings(app, |s| s.told_about_tray = true);
                        notice(
                            app,
                            "CalSync is still syncing",
                            "It keeps running in the tray. Turn on Start on login there so it also syncs after a restart.",
                        );
                    }
                } else {
                    quit(app);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("build the CalSync app");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            engine::stop(app);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_engine_page_stays_in_the_window() {
        let p = 8495;
        let u = |s: &str| Url::parse(s).unwrap();
        assert!(is_inside(&u("http://127.0.0.1:8495/settings"), p));
        assert!(is_inside(&u("http://tauri.localhost/index.html"), p));
        assert!(is_inside(&u("tauri://localhost/index.html"), p));
        assert!(!is_inside(&u("http://127.0.0.1:8484/"), p), "the WSL install is another app");
        assert!(!is_inside(&u("http://localhost:8495/"), p));
        assert!(!is_inside(&u("https://accounts.google.com/o/oauth2/auth"), p));
        assert!(!is_inside(&u("https://microsoft.com/devicelogin"), p));
        assert!(!is_inside(&u("file:///C:/Windows/"), p));
    }

    #[test]
    fn tray_commands_from_the_command_line() {
        let argv = |s: &str| s.split(' ').map(String::from).collect::<Vec<_>>();
        assert_eq!(tray_action(&argv("CalSync.exe --tray sync")), Some("sync"));
        assert_eq!(tray_action(&argv("CalSync.exe --tray rm-rf")), None);
        assert_eq!(tray_action(&argv("CalSync.exe --tray")), None);
        assert_eq!(tray_action(&argv("CalSync.exe --background")), None);
    }
}
