// The engine: calsync-engine (a PyInstaller build of desktop/sidecar/calsync_desktop.py) runs
// `calsync web` on 127.0.0.1:<port>. The app talks to it over its stdin and stdout only:
// commands in ("sync", "pause", "resume", "quit"), status lines out ("@@calsync {json}").
// When the app goes, stdin closes and the engine exits with it, even after a crash.
use serde::Deserialize;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, Runtime};

const PREFIX: &str = "@@calsync ";

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(default)]
pub struct Status {
    pub running: bool,
    pub enabled: bool,
    pub paused: bool,
    pub calendars: u32,
    pub interval: u32,
    pub last_at: Option<i64>,
    pub errors: u32,
    pub problem: Option<String>,
    pub problems: u32,
}

#[derive(Default)]
pub struct Engine {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    pub status: Mutex<Option<Status>>,
    /// Set when the engine has stopped for good (it failed to start, or kept crashing).
    pub failed: Mutex<Option<String>>,
    quitting: AtomicBool,
}

pub fn engine<R: Runtime>(app: &AppHandle<R>) -> tauri::State<'_, Engine> {
    app.state::<Engine>()
}

/// The program and its leading arguments. CALSYNC_DEV_PYTHON (a debug build only) runs the
/// sidecar script from the repo with that Python, so `cargo run` works without PyInstaller.
fn program<R: Runtime>(app: &AppHandle<R>) -> Result<(PathBuf, Vec<PathBuf>), String> {
    #[cfg(debug_assertions)]
    if let Some(py) = std::env::var_os("CALSYNC_DEV_PYTHON") {
        let script = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../sidecar/calsync_desktop.py");
        return Ok((PathBuf::from(py), vec![script]));
    }
    let exe = if cfg!(windows) { "calsync-engine.exe" } else { "calsync-engine" };
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    let path = dir.join("engine").join(exe);
    if path.exists() {
        Ok((path, vec![]))
    } else {
        Err(format!("The CalSync engine is missing from {}. Reinstall CalSync.", path.display()))
    }
}

pub fn start<R: Runtime>(app: &AppHandle<R>) {
    let e = engine(app);
    e.quitting.store(false, Ordering::SeqCst);
    *e.failed.lock().unwrap() = None;
    let desk = crate::desk(app);
    let (port, paused) = (desk.port, desk.settings.lock().unwrap().paused);
    let (exe, args) = match program(app) {
        Ok(p) => p,
        Err(msg) => return give_up(app, msg),
    };
    let mut cmd = Command::new(&exe);
    cmd.args(&args)
        .env("CALSYNC_HOME", desk.dir.join("data"))
        .env("CALSYNC_BIND", "127.0.0.1")
        .env("CALSYNC_PORT", port.to_string())
        .env("CALSYNC_DESKTOP_PAUSED", if paused { "1" } else { "0" })
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env_remove("CALSYNC_PUBLIC_URL")
        .env_remove("CALSYNC_HOSTS")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(debug_assertions)]
    if std::env::var_os("CALSYNC_DEV_PYTHON").is_some() {
        cmd.env("PYTHONPATH", PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(err) => return give_up(app, format!("CalSync's engine didn't start: {err}")),
    };
    log::info!("engine started (pid {}) on 127.0.0.1:{port}", child.id());
    let out = child.stdout.take();
    let err = child.stderr.take();
    *e.stdin.lock().unwrap() = child.stdin.take();
    *e.child.lock().unwrap() = Some(child);

    if let Some(out) = out {
        let app = app.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                if let Some(json) = line.strip_prefix(PREFIX) {
                    if let Ok(s) = serde_json::from_str::<Status>(json) {
                        let changed = {
                            let e = engine(&app);
                            let mut now = e.status.lock().unwrap();
                            let old = now.replace(s.clone());
                            old.as_ref() != Some(&s)
                        };
                        if changed {
                            crate::on_status(&app, &s);
                        }
                    }
                } else {
                    log::info!("engine: {line}");
                }
            }
        });
    }
    if let Some(err) = err {
        std::thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                log::warn!("engine: {line}");
            }
        });
    }
    let app = app.clone();
    std::thread::spawn(move || watch(app, port));
}

/// Waits for the page to answer, opens it in the window, then restarts the engine if it dies.
fn watch<R: Runtime>(app: AppHandle<R>, port: u16) {
    let started = Instant::now();
    let mut ready = false;
    loop {
        std::thread::sleep(Duration::from_millis(if ready { 1000 } else { 150 }));
        let e = engine(&app);
        let exited = {
            let mut child = e.child.lock().unwrap();
            match child.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(code))) => Some(code.to_string()),
                Some(Err(err)) => Some(err.to_string()),
                Some(Ok(None)) => None,
                None => Some("gone".into()),
            }
        };
        if let Some(code) = exited {
            if e.quitting.load(Ordering::SeqCst) {
                return;
            }
            log::warn!("engine exited: {code}");
            *e.child.lock().unwrap() = None;
            *e.stdin.lock().unwrap() = None;
            return restart(&app);
        }
        if !ready && std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(300)).is_ok() {
            ready = true;
            log::info!("engine ready after {:?}", started.elapsed());
            crate::page_ready(&app);
        }
        if !ready && started.elapsed() > Duration::from_secs(60) {
            stop(&app);
            return give_up(&app, "CalSync's engine didn't answer within a minute.".into());
        }
    }
}

/// Up to five restarts in ten minutes, then the window shows what went wrong.
fn restart<R: Runtime>(app: &AppHandle<R>) {
    static RESTARTS: Mutex<Vec<Instant>> = Mutex::new(Vec::new());
    let mut times = RESTARTS.lock().unwrap();
    times.retain(|t| t.elapsed() < Duration::from_secs(600));
    if times.len() >= 5 {
        drop(times);
        return give_up(app, "CalSync's engine keeps stopping. The log file has the details.".into());
    }
    times.push(Instant::now());
    let wait = Duration::from_secs(2u64.pow(times.len() as u32));
    drop(times);
    std::thread::sleep(wait);
    start(app);
}

fn give_up<R: Runtime>(app: &AppHandle<R>, msg: String) {
    log::error!("{msg}");
    *engine(app).failed.lock().unwrap() = Some(msg.clone());
    crate::engine_failed(app, &msg);
}

pub fn send<R: Runtime>(app: &AppHandle<R>, word: &str) {
    let e = engine(app);
    let mut stdin = e.stdin.lock().unwrap();
    if let Some(pipe) = stdin.as_mut() {
        if let Err(err) = writeln!(pipe, "{word}").and_then(|_| pipe.flush()) {
            log::warn!("engine stdin: {err}");
        }
    }
}

/// Quit: ask nicely, then make sure.
pub fn stop<R: Runtime>(app: &AppHandle<R>) {
    let e = engine(app);
    e.quitting.store(true, Ordering::SeqCst);
    send(app, "quit");
    *e.stdin.lock().unwrap() = None;
    let child = e.child.lock().unwrap().take();
    if let Some(mut c) = child {
        let deadline = Instant::now() + Duration::from_millis(1500);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = c.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
}
