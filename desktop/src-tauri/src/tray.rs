// The tray (menu bar on a Mac): Open CalSync, Sync now, Pause, the last sync and its problems,
// the two settings, and Quit. A left click opens the window.
use crate::engine::Status;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Runtime};

pub const ID: &str = "calsync";

pub struct Items<R: Runtime> {
    sync: MenuItem<R>,
    pause: MenuItem<R>,
    last: MenuItem<R>,
    problem: MenuItem<R>,
    pub background: CheckMenuItem<R>,
    pub login: CheckMenuItem<R>,
}

pub fn create<R: Runtime>(app: &AppHandle<R>, keep_background: bool, login: bool) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open CalSync", true, None::<&str>)?;
    let sync = MenuItem::with_id(app, "sync", "Sync now", false, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause syncing", false, None::<&str>)?;
    let last = MenuItem::with_id(app, "last", "Starting…", false, None::<&str>)?;
    let problem = MenuItem::with_id(app, "problem", "No problems", false, None::<&str>)?;
    let background = CheckMenuItem::with_id(app, "background", "Keep syncing in the background", true, keep_background, None::<&str>)?;
    let login_item = CheckMenuItem::with_id(app, "login", "Start on login", true, login, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit CalSync", true, None::<&str>)?;
    let sep = || PredefinedMenuItem::separator(app);
    let menu = Menu::with_items(
        app,
        &[&open, &sync, &pause, &sep()?, &last, &problem, &sep()?, &background, &login_item, &sep()?, &quit],
    )?;
    let mut b = TrayIconBuilder::with_id(ID).tooltip("CalSync").menu(&menu).show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon() {
        b = b.icon(icon.clone());
    }
    b.on_menu_event(|app, event| crate::tray_command(app, event.id().as_ref()))
    .on_tray_icon_event(|tray, event| {
        if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
            crate::show_main(tray.app_handle());
        }
    })
    .build(app)?;
    app.manage(Items { sync, pause, last, problem, background, login: login_item });
    Ok(())
}

pub fn items<R: Runtime>(app: &AppHandle<R>) -> Option<tauri::State<'_, Items<R>>> {
    app.try_state::<Items<R>>()
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

pub fn ago(secs: i64) -> String {
    match secs.max(0) {
        s if s < 60 => "just now".into(),
        s if s < 3600 => format!("{} min ago", s / 60),
        s if s < 86_400 => format!("{} h ago", s / 3600),
        s if s < 2 * 86_400 => "yesterday".into(),
        s => format!("{} days ago", s / 86_400),
    }
}

/// The words for the status line, the problem line and the tooltip.
pub fn words(s: Option<&Status>, failed: Option<&str>, at: i64) -> (String, String, String) {
    if let Some(msg) = failed {
        return ("CalSync's engine stopped".into(), cut(msg, 70), "CalSync: the engine stopped".into());
    }
    let Some(s) = s else {
        return ("Starting…".into(), "No problems".into(), "CalSync".into());
    };
    let last = s.last_at.map(|t| ago(at - t));
    let line = if s.running {
        "Syncing now…".into()
    } else if s.calendars < 2 {
        "Not set up yet: open CalSync to add calendars".into()
    } else if !s.enabled {
        "Sync is off: start it in CalSync".into()
    } else if s.paused {
        match &last {
            Some(l) => format!("Paused · last sync {l}"),
            None => "Paused".into(),
        }
    } else {
        match &last {
            Some(l) => format!("Last sync {l}"),
            None => "Waiting for the first sync".into(),
        }
    };
    let problem = match (&s.problem, s.problems.max(s.errors)) {
        (_, 0) => "No problems".into(),
        (Some(p), 1) => cut(&format!("Problem: {p}"), 70),
        (Some(p), n) => cut(&format!("{n} problems: {p}"), 70),
        (None, n) => format!("{n} problems in the last sync"),
    };
    let tip = if s.problems.max(s.errors) > 0 {
        "CalSync: the last sync had problems".to_string()
    } else if s.paused {
        "CalSync: paused".into()
    } else if s.enabled {
        format!("CalSync: {}", line.to_lowercase().replace("last sync", "synced"))
    } else {
        "CalSync".into()
    };
    (line, problem, cut(&tip, 120))
}

fn cut(s: &str, max: usize) -> String {
    let one_line = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if one_line.chars().count() <= max {
        one_line
    } else {
        one_line.chars().take(max - 1).collect::<String>() + "…"
    }
}

pub fn refresh<R: Runtime>(app: &AppHandle<R>) {
    let e = crate::engine::engine(app);
    let status = e.status.lock().unwrap().clone();
    let failed = e.failed.lock().unwrap().clone();
    let (line, problem, tip) = words(status.as_ref(), failed.as_deref(), now());
    {
        static SHOWN: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());
        let paused = status.as_ref().is_some_and(|s| s.paused);
        let now_shown = format!("{line} | {problem} | {}", if paused { "Resume syncing" } else { "Pause syncing" });
        let mut shown = SHOWN.lock().unwrap();
        if *shown != now_shown {
            log::info!("tray: {now_shown}");
            *shown = now_shown;
        }
    }
    if let Some(items) = items(app) {
        let _ = items.last.set_text(line);
        let _ = items.problem.set_text(problem);
        let ok = failed.is_none() && status.as_ref().is_some_and(|s| s.enabled);
        let _ = items.sync.set_enabled(ok && !status.as_ref().is_some_and(|s| s.running));
        let _ = items.pause.set_enabled(ok);
        let paused = status.as_ref().map(|s| s.paused).unwrap_or_else(|| crate::desk(app).settings.lock().unwrap().paused);
        let _ = items.pause.set_text(if paused { "Resume syncing" } else { "Pause syncing" });
    }
    if let Some(tray) = app.tray_by_id(ID) {
        let _ = tray.set_tooltip(Some(tip));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok() -> Status {
        Status { enabled: true, calendars: 3, interval: 5, last_at: Some(1000), ..Default::default() }
    }

    #[test]
    fn says_when_it_last_synced() {
        assert_eq!(words(Some(&ok()), None, 1030).0, "Last sync just now");
        assert_eq!(words(Some(&ok()), None, 1000 + 300).0, "Last sync 5 min ago");
        assert_eq!(words(Some(&ok()), None, 1000 + 300).2, "CalSync: synced 5 min ago");
        let paused = Status { paused: true, ..ok() };
        assert_eq!(words(Some(&paused), None, 1000 + 7200).0, "Paused · last sync 2 h ago");
    }

    #[test]
    fn names_the_first_problem() {
        let bad = Status { errors: 2, problems: 2, problem: Some("google quartzi: HTTP 401 Unauthorized".into()), ..ok() };
        let (_, p, tip) = words(Some(&bad), None, 1010);
        assert_eq!(p, "2 problems: google quartzi: HTTP 401 Unauthorized");
        assert_eq!(tip, "CalSync: the last sync had problems");
        let long = Status { problems: 1, errors: 1, problem: Some("x".repeat(200)), ..ok() };
        assert_eq!(words(Some(&long), None, 1010).1.chars().count(), 70);
    }

    #[test]
    fn before_setup_it_points_to_the_window() {
        let new = Status { calendars: 0, ..Default::default() };
        assert_eq!(words(Some(&new), None, 0).0, "Not set up yet: open CalSync to add calendars");
        let off = Status { enabled: false, ..ok() };
        assert_eq!(words(Some(&off), None, 0).0, "Sync is off: start it in CalSync");
        assert_eq!(words(None, Some("missing"), 0).0, "CalSync's engine stopped");
    }
}
