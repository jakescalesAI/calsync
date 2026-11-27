"""The desktop app's engine: `calsync web` on 127.0.0.1, run by the CalSync app (desktop/src-tauri).

The app starts this with CALSYNC_HOME (its own app-data folder), CALSYNC_BIND=127.0.0.1 and CALSYNC_PORT set,
and talks to it over stdin and stdout, so there is no second port to guard:

  stdin, one word a line:  sync | pause | resume | status | quit
  stdout:                  "@@calsync " + a JSON status line, at start, every few seconds and after each command

When stdin closes (the app quit or crashed), this exits at once, so it never outlives the app.
The engine and the web page are unchanged; the only difference from `calsync web` is the scheduler,
which here also honours Pause from the tray.
"""
import calendar, json, os, sys, threading, time

def default_home():
    """The same folder the app uses (lib.rs data_dir), so `calsync-engine reset-password` in a terminal just works."""
    app = "app.getcalsync.desktop"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, app, "data")


os.environ.setdefault("CALSYNC_HOME", default_home())
if os.environ.get("CALSYNC_BIND", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost"):
    sys.exit("The desktop engine only listens on 127.0.0.1.")
os.environ["CALSYNC_BIND"] = "127.0.0.1"
for name in ("CALSYNC_PUBLIC_URL", "CALSYNC_HOSTS"):     # nothing from outside widens who can reach it
    os.environ.pop(name, None)

import calsync as engine          # noqa: E402  (reads CALSYNC_HOME at import)
import calsync_web as web         # noqa: E402

PREFIX = "@@calsync "
paused = threading.Event()
if os.environ.get("CALSYNC_DESKTOP_PAUSED") == "1":
    paused.set()
out_lock = threading.Lock()
TICK = 5


def status():
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    last = engine.load_json(engine.LASTRUN, None) or {}
    at = last.get("at")
    try:
        at_epoch = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")) if at else None
    except (TypeError, ValueError):
        at_epoch = None
    problems = [str(p).lstrip("! ").strip() for p in last.get("problems") or []]
    return {
        "port": web.PORT,
        "running": bool(web.run_state.get("running")),
        "enabled": bool(cfg.get("enabled")),
        "paused": paused.is_set(),
        "calendars": len(cfg.get("calendars") or []),
        "interval": cfg.get("interval_minutes", 5),
        "last_at": at_epoch,
        "errors": int(last.get("errors") or 0),
        "problem": problems[0] if problems else None,
        "problems": len(problems),
    }


def say(obj):
    line = PREFIX + json.dumps(obj, separators=(",", ":"))
    with out_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            os._exit(0)


def say_status():
    try:
        say(status())
    except Exception as e:      # a half-written config mid-save; the next tick will do
        say({"port": web.PORT, "status_error": web.short_error(e)})


def scheduler(stop):
    """calsync_web.scheduler, plus Pause. `last` starts at 0 so the first run is right after start."""
    last = 0.0
    while not stop.wait(15):
        if paused.is_set():
            continue
        cfg = engine.load_json(engine.CONFIG, {"calendars": []})
        every = cfg.get("interval_minutes", 5) * 60
        if cfg.get("enabled") and time.time() - last >= every and web.run_in_background():
            last = time.time()


def commands():
    for raw in sys.stdin:
        word = raw.strip().lower()
        if word == "sync":      # only once sync is started on the page, which comes after its preview
            if engine.load_json(engine.CONFIG, {}).get("enabled"):
                web.run_in_background()
        elif word == "pause":
            paused.set()
        elif word == "resume":
            paused.clear()
        elif word == "quit":
            break
        say_status()
    os._exit(0)     # stdin closed: the app is gone


def ticker():
    while True:
        say_status()
        time.sleep(TICK)


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(errors="replace")
    web.scheduler = scheduler   # serve() starts the module's scheduler by name
    threading.Thread(target=commands, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    return web.serve()


if __name__ == "__main__":
    if len(sys.argv) > 1:       # `calsync-engine reset-password`, `status`, ...: the plain CLI on the app's data
        engine.cli()
    raise SystemExit(main())
