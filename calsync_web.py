#!/usr/bin/env python3
"""The web setup page and background sync: `calsync web`, or the Docker image.

No JavaScript and no outside files, so a strict content policy holds. Everything a page shows goes
through esc(). Tokens, client secrets, CalDAV passwords and .ics links are stored by the engine and never sent to a page.

  CALSYNC_BIND        address to listen on (default 127.0.0.1; the Docker image uses 0.0.0.0)
  CALSYNC_PORT        port (default 8484)
  CALSYNC_PUBLIC_URL  https address, if you put it behind your own proxy; used as Google's redirect
  CALSYNC_HOSTS       extra host names allowed to reach it, comma separated
"""
import hashlib, hmac, html, http.cookies, http.server, ipaddress, json, os, re, secrets, signal, threading, time, urllib.parse
import calsync as engine

PORT = int(os.environ.get("CALSYNC_PORT") or 8484)
BIND = os.environ.get("CALSYNC_BIND") or "127.0.0.1"
PUBLIC_URL = (os.environ.get("CALSYNC_PUBLIC_URL") or "").rstrip("/")
EXTRA_HOSTS = {h.strip().lower() for h in (os.environ.get("CALSYNC_HOSTS") or "").split(",") if h.strip()}
SESSION_DAYS = 30
MAX_BODY = 64 * 1024
INTERVALS = (2, 5, 10, 15, 30, 60)
ATTEMPTS_PER_MINUTE = 5            # wrong passwords or setup codes from one address
EVERYONE_PER_MINUTE = {"login": 30, "setup": 10}   # and from all addresses together, so many addresses don't help
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")

state_lock = threading.Lock()
pending_google = {}     # state -> label, redirect, origin, expires
pending_ms = {}         # flow id -> label, tenant, device_code, user_code, link, expires, status
failed_logins = {}      # (kind, ip or "*") -> [times of tries]
first_run_lock = threading.Lock()
setup_code = secrets.token_hex(4)
last_preview = {}       # session key -> the preview shown next
run_state = {"running": False}


def esc(v):
    return html.escape(str(v), quote=True)


def web_file():
    return os.path.join(engine.SECRETS, "web.json")


def web_config():
    return engine.load_json(web_file(), None)


def hash_password(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024).hex()


def set_password(password):
    salt = secrets.token_hex(16)
    engine.ensure_home()
    engine.save_json(web_file(), {"salt": salt, "hash": hash_password(password, salt), "key": secrets.token_hex(32)})  # a new key signs everyone out


def sign(key, text):
    return hmac.new(bytes.fromhex(key), text.encode(), hashlib.sha256).hexdigest()


def make_session(cfg):
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{sign(cfg['key'], 'session.' + expires)}"


def session_ok(cfg, cookie):
    expires, _, mac = (cookie or "").partition(".")
    return bool(cfg and expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(mac, sign(cfg["key"], "session." + expires)))


def host_allowed(host):
    """A page on someone else's domain can point its name at this machine; only real names and bare addresses pass."""
    name = re.sub(r":\d+$", "", (host or "").strip().lower()).strip("[]")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost") or name in EXTRA_HOSTS or name == urllib.parse.urlparse(PUBLIC_URL).hostname:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def take_try(kind, ip):
    """Reserve one password or setup-code try before checking it, so parallel requests can't slip past the cap.
    Returns the try's stamp, or None when this address or everyone together used up the last minute."""
    now = time.time()
    with state_lock:
        mine = [t for t in failed_logins.get((kind, ip), []) if now - t < 60]
        everyone = [t for t in failed_logins.get((kind, "*"), []) if now - t < 60]
        if len(mine) >= ATTEMPTS_PER_MINUTE or len(everyone) >= EVERYONE_PER_MINUTE[kind]:
            failed_logins[(kind, ip)], failed_logins[(kind, "*")] = mine, everyone
            return None
        failed_logins[(kind, ip)], failed_logins[(kind, "*")] = mine + [now], everyone + [now]
        return now


def give_back(kind, ip, stamp):
    """A right answer doesn't count against anyone."""
    with state_lock:
        for key in ((kind, ip), (kind, "*")):
            if stamp in failed_logins.get(key, []):
                failed_logins[key].remove(stamp)


def is_loopback(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# ---------- pages ----------

CSS = """
:root{color-scheme:dark;--bg:#000;--card:#0b0b0c;--ink:#fff;--soft:#b0b0b0;--line:#262626;--accent:#fff;--accent-ink:#000;--bad:#ff9d9d;--note:#151516}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:18px;margin-right:auto}header a{color:var(--soft);text-decoration:none}header a:hover{color:var(--ink)}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;line-height:1.2;margin:0 0 16px}h2{font-size:19px;margin:32px 0 12px}
section,.row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 12px}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}.row>div{min-width:0;overflow-wrap:anywhere}
label{display:block;font-weight:600;margin:12px 0 4px}label.check{font-weight:400;display:flex;gap:8px;align-items:flex-start;margin:6px 0}
input[type=text],input[type=password],input[type=url],select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button,.button{display:inline-block;padding:10px 18px;border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
button.plain,.button.plain{background:transparent;color:var(--ink);border-color:var(--line)}button.danger{background:transparent;color:var(--bad);border-color:var(--bad)}
form.inline{display:inline}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.actions.flat{margin:0}.soft{color:var(--soft)}.bad{color:var(--bad);font-weight:600}
.note{background:var(--note);border-color:transparent}ol{padding-left:22px}li{margin:6px 0}code{background:var(--note);padding:2px 5px;border-radius:5px;overflow-wrap:anywhere}
.code{font:700 28px/1.2 ui-monospace,monospace;letter-spacing:2px}pre{background:var(--note);padding:12px;border-radius:8px;overflow:auto;max-height:420px;font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
"""


def page(title, body, signed_in=False, flash=None, refresh=None):
    nav = '<a href="/">Sync</a> <a href="/connect">Add a calendar</a> <a href="/settings">Settings</a>' if signed_in else ""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    note = f'<section class="note" role="status">{esc(flash)}</section>' if flash else ""
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{esc(title)} · calsync</title><link rel="stylesheet" href="/app.css"><link rel="icon" href="data:,">'
            f'<header><b>calsync</b>{nav}</header><main>{note}{body}</main></html>')


GOOGLE_STEPS = """
<ol>
<li>Open <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">console.cloud.google.com</a> and create a project called calsync.</li>
<li>Go to APIs &amp; Services, Library, find <b>Google Calendar API</b> and click Enable.</li>
<li>Go to Google Auth Platform and click Get started. Name it calsync, pick your email, choose <b>External</b>.</li>
<li>Go to Audience and click <b>Publish app</b>. Unverified is fine. An app left in Testing signs you out every 7 days.</li>
<li>Go to Clients, Create client, type <b>Desktop app</b>. Copy the client ID and secret here.</li>
</ol>"""

MICROSOFT_STEPS = """
<ol>
<li>Open <a href="https://entra.microsoft.com/" target="_blank" rel="noreferrer">entra.microsoft.com</a>, then App registrations, New registration.</li>
<li>Name it calsync and choose <b>Accounts in any organizational directory and personal Microsoft accounts</b>. Leave the redirect empty.</li>
<li>Under Authentication, turn on <b>Allow public client flows</b> and save.</li>
<li>Under API permissions, add Microsoft Graph, Delegated, <b>Calendars.ReadWrite</b> and <b>offline_access</b>.</li>
<li>Copy the Application (client) ID here.</li>
</ol>"""


def describe_run(s):
    if not s:
        return "No sync has run yet."
    bits = f"{s.get('created', 0)} added, {s.get('updated', 0)} updated, {s.get('deleted', 0)} removed"
    return f"{bits}, at {s.get('at', '')} UTC"


def dashboard(csrf, flash=None, preview=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    cals, accounts = cfg["calendars"], engine._accounts()
    last = engine.load_json(engine.LASTRUN, None)
    out = ["<h1>Your calendars</h1>"]
    if not accounts:
        return page("Sync", out[0] + '<section><p>Nothing is connected yet. Start by adding your first calendar.</p>'
                    '<a class="button" href="/connect">Add a calendar</a></section>', True, flash)
    names = [c["name"] for c in cals]
    for c in cals:
        flags = [f for f, on in (("Busy only", c.get("busy_only")), ("Never written to", c.get("read_only"))) if on]
        flags += [f"Skips {x}" for x in c.get("skip_sources", [])]
        skip = "".join(f'<label class="check"><input type="checkbox" name="skip" value="{esc(n)}"{" checked" if n in c.get("skip_sources", []) else ""}> Don\'t copy {esc(n)} here</label>'
                       for n in names if n != c["name"])
        out.append(f'''<section><b>{esc(c.get("label") or c["name"])}</b> <span class="soft">{esc(c["account"])}{" · " + esc(", ".join(flags)) if flags else ""}</span>
<details><summary>Options</summary><form method="post" action="/calendar/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}">
<label class="check"><input type="checkbox" name="busy_only"{" checked" if c.get("busy_only") else ""}> Copies here only say Busy</label>
<label class="check"><input type="checkbox" name="read_only"{" checked" if c.get("read_only") else ""}> Never write to this calendar</label>
{skip}<div class="actions"><button>Save</button></div></form>
<div class="actions"><form class="inline" method="post" action="/calendar/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="plain">Stop syncing this calendar</button></form>
<form class="inline" method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="danger">Delete the copies calsync made here</button></form></div></details></section>''')
    if len(cals) < 2:
        out.append('<section class="note">Add at least two calendars and calsync can start.</section>')
    out.append('<div class="actions"><a class="button plain" href="/connect">Add a calendar</a></div><h2>Sync</h2>')
    state = "Running in the background every " + str(cfg.get("interval_minutes", 5)) + " minutes." if cfg.get("enabled") else "Not running yet. Preview first, then start it."
    out.append(f'<section><p>{esc(state)}</p><p class="soft">Last run: {esc(describe_run(last))}</p>')
    if last and last.get("problems"):
        out.append('<p class="bad">Last run had problems:</p><pre>' + esc("\n".join(last["problems"])) + "</pre>")
    if last and last.get("skipped_deletes"):
        out.append(f'<p class="bad">A run wanted to delete {esc(last["skipped_deletes"])} copies, more than half of a calendar, so it stopped and waited for you. Preview to see them.</p>')
    if len(cals) >= 2:
        hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
        out.append(f'''<div class="actions"><form class="inline" method="post" action="/preview">{hidden}<button{" class=plain" if cfg.get("enabled") else ""}>Preview changes</button></form>''')
        if cfg.get("enabled"):
            out.append(f'<form class="inline" method="post" action="/run">{hidden}<button>Sync now</button></form><form class="inline" method="post" action="/pause">{hidden}<button class="plain">Pause</button></form>')
        out.append("</div>")
    out.append("</section>")
    if preview is not None:
        out.append(f'<h2>Preview</h2><section><p>{esc(preview["created"])} to add, {esc(preview["updated"])} to update, {esc(preview["deleted"])} to remove. Nothing has changed yet.</p>')
        if preview.get("skipped_deletes"):
            out.append(f'<p class="bad">{esc(preview["skipped_deletes"])} deletes would be held back by the safety stop.</p>')
        out.append("<pre>" + esc("\n".join(preview["lines"][:400]) or "Nothing to change.") + "</pre>")
        hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
        out.append(f'<div class="actions"><form class="inline" method="post" action="/start">{hidden}<button>Apply and keep syncing</button></form>')
        if preview.get("skipped_deletes"):
            out.append(f'<form class="inline" method="post" action="/run">{hidden}<input type="hidden" name="force" value="1"><button class="danger">Allow these deletes</button></form>')
        out.append("</div></section>")
    return page("Sync", "".join(out), True, flash)


def connect_page(csrf, flash=None):
    accounts = engine._accounts()
    rows = []
    for label, a in accounts.items():
        kind = a.get("service") or {"google": "Google", "outlook": "Microsoft", "caldav": "CalDAV", "ics": ".ics link"}.get(a["provider"], "")
        rows.append(f'''<div class="row"><div><b>{esc(label)}</b> <span class="soft">{kind}</span></div><div class="actions flat"><a class="button" href="/account?label={urllib.parse.quote(label)}">Choose calendars</a>
<form class="inline" method="post" action="/account/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="label" value="{esc(label)}"><button class="plain">Sign out</button></form></div></div>''')
    existing = ("<h2>Signed in</h2>" + "".join(rows)) if rows else ""
    caldav = "".join(f'''<div class="row"><div><b>{"Another CalDAV server" if k == "other" else v[0]}</b></div><a class="button" href="/connect/caldav?p={k}">Connect</a></div>'''
                     for k, v in engine.CALDAV_SERVERS.items())
    return page("Add a calendar", f'''<h1>Add a calendar</h1>{existing}<h2>Connect an account</h2>
<div class="row"><div><b>Google Calendar</b></div><a class="button" href="/connect/google">Connect</a></div>
<div class="row"><div><b>Outlook or Microsoft 365</b></div><a class="button" href="/connect/microsoft">Connect</a></div>
{caldav}
<div class="row"><div><b>Published .ics link</b><br><span class="soft">Read-only: its events are copied out, nothing is written back. For Proton Calendar, an Exchange server, or an organization that blocks outside apps.</span></div><a class="button" href="/connect/ics">Add link</a></div>''', True, flash)


def google_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "google-client.json"), None)
    if client:
        return page("Connect Google", f'''<h1>Connect Google</h1><section><form method="post" action="/connect/google/start"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="work">
<div class="actions"><button>Continue</button></div></form></section>
<section><p>Your Google sign-in app is saved. Replace it only if you made a new one.</p>{client_form(csrf)}</section>''', True, flash)
    return page("Connect Google", f'<h1>Connect Google</h1><section><p>Google needs a sign-in app of your own. It\'s free and takes about 10 minutes. You only do it once, for all your Google accounts.</p>{GOOGLE_STEPS}{client_form(csrf)}</section>', True, flash)


def client_form(csrf):
    return f'''<form method="post" action="/connect/google/client" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Client ID</label><input type="text" id="cid" name="client_id" required><label for="sec">Client secret</label><input type="password" id="sec" name="client_secret" required>
<div class="actions"><button>Save</button></div></form>'''


def microsoft_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "microsoft-client.json"), None)
    steps = "" if client else f"<p>Microsoft needs a sign-in app of your own. It's free and you only make it once.</p>{MICROSOFT_STEPS}"
    cid = esc(client["client_id"]) if client else ""
    return page("Connect Microsoft", f'''<h1>Connect Outlook or Microsoft 365</h1><section>{steps}<form method="post" action="/connect/microsoft/start" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Application (client) ID</label><input type="text" id="cid" name="client_id" required value="{cid}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="school">
<div class="actions"><button>Continue</button></div></form></section>''', True, flash)


def ics_page(csrf, flash=None):
    return page("Add an .ics link", f'''<h1>Add a published .ics link</h1><section><p>calsync saves this link and never shows it again, since anyone who has it can read that calendar.</p>
<form method="post" action="/connect/ics/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="classes">
<label for="url">Calendar link</label><input type="password" id="url" name="url" required>
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash)


def caldav_page(kind, csrf, flash=None):
    name, server, note = engine.CALDAV_SERVERS[kind]
    address = "" if server else '<label for="server">Server address</label><input type="text" id="server" name="server" required placeholder="https://cloud.example.com">'
    return page(f"Connect {name}", f'''<h1>Connect {name}</h1><section><p>{note}</p><p>calsync saves this password on this machine and never shows it again.</p>
<form method="post" action="/connect/caldav/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="kind" value="{kind}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="personal">
{address}<label for="username">Username</label><input type="text" id="username" name="username" required>
<label for="password">Password</label><input type="password" id="password" name="password" required autocomplete="new-password">
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash)


def account_page(label, csrf, flash=None):
    try:
        found = engine.account_calendars(label)
    except Exception as e:
        return page("Choose calendars", f'<h1>{esc(label)}</h1><section class="bad">Couldn\'t read this account\'s calendars: {esc(short_error(e))}</section>', True, flash)
    used = {(c.get("account"), c.get("calendar_id")) for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
    rows = []
    for i, c in enumerate(found):
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{label}-{c['name']}" if len(found) > 1 else label).strip("-_")[:40] or f"{label}-{i}"
        if (label, c["id"]) in used:
            rows.append(f'<div class="row"><div><b>{esc(c["name"])}</b> <span class="soft">already syncing</span></div></div>')
            continue
        ro = "" if c["writable"] else '<p class="soft">calsync can read this one but not write to it.</p>'
        rows.append(f'''<section><b>{esc(c["name"])}</b>{ro}<form method="post" action="/calendar/add"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="account" value="{esc(label)}"><input type="hidden" name="calendar_id" value="{esc(c["id"])}">
<label for="n{i}">Name in calsync</label><input type="text" id="n{i}" name="name" value="{esc(slug)}" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*">
{'<label class="check"><input type="checkbox" name="busy_only"> Copies here only say Busy</label>' if c["writable"] else ""}
<div class="actions"><button>Sync this calendar</button></div></form></section>''')
    return page("Choose calendars", f'<h1>{esc(label)}: choose calendars</h1>{"".join(rows) or "<section>This account has no calendars.</section>"}<div class="actions"><a class="button plain" href="/">Back to sync</a></div>', True, flash)


def settings_page(csrf, flash=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    current = cfg.get("interval_minutes", 5)
    opts = "".join(f'<option value="{m}"{" selected" if m == current else ""}>Every {m} minutes</option>' for m in INTERVALS)
    return page("Settings", f'''<h1>Settings</h1><section><form method="post" action="/settings/save"><input type="hidden" name="csrf" value="{csrf}">
<label for="iv">How often to sync</label><select id="iv" name="interval">{opts}</select>
<label for="tz">Your time zone</label><input type="text" id="tz" name="timezone" value="{esc(cfg.get("timezone") or engine.tz_name())}" required>
<div class="actions"><button>Save</button></div></form></section>
<h2>Change password</h2><section><form method="post" action="/settings/password" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="old">Current password</label><input type="password" id="old" name="old" required><label for="new">New password</label><input type="password" id="new" name="new" required minlength="10">
<div class="actions"><button>Change password</button></div></form></section>
<div class="actions"><form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button class="plain">Sign out</button></form>
<form class="inline" method="post" action="/logout/everywhere"><input type="hidden" name="csrf" value="{csrf}"><button class="plain">Sign out on every device</button></form></div>''', True, flash)


def short_error(e):
    """Errors from the providers can be long; tokens never appear in them, since http() only reports the status and body."""
    return str(e)[:300]


# ---------- background sync ----------

def run_in_background(force=False):
    with state_lock:
        if run_state["running"]:
            return False
        run_state["running"] = True

    def job():
        try:
            engine.run_once(force=force, log=lambda line: None)     # titles stay out of the container's logs
        except Exception as e:
            engine.save_json(engine.LASTRUN, {"at": engine.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": 1, "failed": [], "problems": [f"! {short_error(e)}"]})
        finally:
            with state_lock:
                run_state["running"] = False
    threading.Thread(target=job, daemon=True).start()
    return True


def scheduler(stop):
    last = 0.0
    while not stop.wait(15):
        cfg = engine.load_json(engine.CONFIG, {"calendars": []})
        every = cfg.get("interval_minutes", 5) * 60
        if cfg.get("enabled") and time.time() - last >= every and run_in_background():
            last = time.time()


def tidy_flows():
    now = time.time()
    with state_lock:
        for flows in (pending_google, pending_ms):
            for key in [k for k, f in flows.items() if f["expires"] < now]:
                del flows[key]
        for key in [k for k, times in failed_logins.items() if not any(now - t < 60 for t in times)]:
            del failed_logins[key]


def poll_microsoft(flow_id):
    while True:
        with state_lock:
            flow = pending_ms.get(flow_id)
        if not flow or flow["expires"] < time.time():
            return
        time.sleep(flow["interval"])
        try:
            if engine.microsoft_device_poll(flow["label"], flow["device_code"], flow["tenant"]):
                flow["status"] = "done"
                return
        except Exception as e:
            flow["status"], flow["problem"] = "failed", short_error(e)
            return


# ---------- requests ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "calsync"
    sys_version = ""

    def log_message(self, *a):      # the query string can hold a sign-in code; keep it out of the logs
        pass

    def send(self, code, body, kind="text/html; charset=utf-8", headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, headers=()):
        self.send(303, "", headers=[("Location", to), *headers])

    def cookie(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        return jar["calsync_session"].value if "calsync_session" in jar else None

    def session_header(self, value, max_age):
        secure = "; Secure" if PUBLIC_URL.startswith("https://") else ""
        return ("Set-Cookie", f"calsync_session={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")

    def read_form(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if size < 0 or size > MAX_BODY:     # a negative length would read until the client hangs up
            return None
        return urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8", "replace"), keep_blank_values=True)

    def csrf(self):
        return sign(web_config()["key"], "csrf." + (self.cookie() or ""))

    def same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urllib.parse.urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def do_GET(self):
        tidy_flows()
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":     # before the host check, so a container's health probe always works
            return self.send(200, "ok", "text/plain")
        if not host_allowed(self.headers.get("Host")):
            return self.send(400, "This host name isn't allowed. Add it to CALSYNC_HOSTS.", "text/plain")
        if url.path == "/app.css":
            return self.send(200, CSS, "text/css")
        if url.path == "/" and ("state" in q or "code" in q or "error" in q):
            return self.google_callback(q)
        cfg = web_config()
        if not cfg:
            return self.send(200, self.first_run_page())
        if not session_ok(cfg, self.cookie()):
            return self.send(200, login_page())
        csrf = self.csrf()
        flash = q.get("m", [None])[0]
        flash = FLASHES.get(flash)
        if url.path == "/":
            preview = None
            with state_lock:
                preview = last_preview.pop(self.cookie(), None)
            return self.send(200, dashboard(csrf, flash, preview))
        if url.path == "/connect":
            return self.send(200, connect_page(csrf, flash))
        if url.path == "/connect/google":
            return self.send(200, google_page(csrf, flash))
        if url.path == "/connect/microsoft":
            return self.send(200, microsoft_page(csrf, flash))
        if url.path == "/connect/microsoft/wait":
            return self.microsoft_wait(q.get("flow", [""])[0])
        if url.path == "/connect/caldav" and q.get("p", [""])[0] in engine.CALDAV_SERVERS:
            return self.send(200, caldav_page(q["p"][0], csrf, flash))
        if url.path == "/connect/ics":
            return self.send(200, ics_page(csrf, flash))
        if url.path == "/account" and q.get("label", [""])[0] in engine._accounts():
            return self.send(200, account_page(q["label"][0], csrf, flash))
        if url.path == "/settings":
            return self.send(200, settings_page(csrf, flash))
        self.send(404, page("Not found", "<h1>Page not found</h1>", True))

    def do_POST(self):
        tidy_flows()
        url = urllib.parse.urlparse(self.path).path
        if not host_allowed(self.headers.get("Host")) or not self.same_origin():
            return self.send(400, "Blocked: this request came from another site.", "text/plain")
        form = self.read_form()
        if form is None:
            return self.send(413, "Too large.", "text/plain")
        get = lambda k, d="": form.get(k, [d])[0]
        cfg = web_config()
        if url == "/first-run":
            return self.first_run(get)
        if not cfg:
            return self.redirect("/")
        if url == "/login":
            return self.login(cfg, get)
        if not session_ok(cfg, self.cookie()):
            return self.redirect("/")
        if not hmac.compare_digest(get("csrf"), self.csrf()):
            return self.send(400, "This page expired. Go back and reload it.", "text/plain")
        try:
            return self.action(url, get, form, cfg)
        except Exception as e:
            return self.send(200, page("Problem", f'<h1>That didn\'t work</h1><section class="bad">{esc(short_error(e))}</section><a class="button plain" href="/">Back</a>', True))

    def action(self, url, get, form, cfg):
        csrf = self.csrf()
        if url == "/logout":
            return self.redirect("/", [self.session_header("", 0)])
        if url == "/logout/everywhere":     # a new signing key ends every session, this one included
            engine.save_json(web_file(), dict(cfg, key=secrets.token_hex(32)))
            return self.redirect("/", [self.session_header("", 0)])
        if url == "/connect/google/client":
            cid, sec = get("client_id").strip(), get("client_secret").strip()
            if not cid or not sec:
                return self.send(200, google_page(csrf, "Add both the client ID and the secret."))
            engine.ensure_home()
            engine.save_json(os.path.join(engine.SECRETS, "google-client.json"), {"client_id": cid, "client_secret": sec})
            return self.redirect("/connect/google?m=saved")
        if url == "/connect/google/start":
            return self.google_start(get("label").strip(), csrf)
        if url == "/connect/google/paste":
            return self.google_paste(get("address"))
        if url == "/connect/microsoft/start":
            label, cid = get("label").strip(), get("client_id").strip()
            if not engine.NAME_OK.fullmatch(label) or not cid:
                return self.send(200, microsoft_page(csrf, "Use letters, numbers, - and _ for the name, and add the client ID."))
            engine.ensure_home()
            engine.save_json(os.path.join(engine.SECRETS, "microsoft-client.json"), {"client_id": cid})
            r = engine.microsoft_device_start()
            flow = secrets.token_urlsafe(16)
            with state_lock:
                pending_ms[flow] = {"label": label, "tenant": "common", "device_code": r["device_code"], "user_code": r["user_code"], "link": r["verification_uri"],
                                    "expires": time.time() + int(r.get("expires_in", 900)), "interval": int(r.get("interval", 5)), "status": "waiting"}
            threading.Thread(target=poll_microsoft, args=(flow,), daemon=True).start()
            return self.redirect("/connect/microsoft/wait?flow=" + flow)
        if url == "/connect/caldav/save" and get("kind") in engine.CALDAV_SERVERS:
            kind, label = get("kind"), get("label").strip()
            name, server, _ = engine.CALDAV_SERVERS[kind]
            if not engine.NAME_OK.fullmatch(label):
                return self.send(200, caldav_page(kind, csrf, "Use letters, numbers, - and _ for the name."))
            try:
                engine.caldav_connect(label, server or get("server"), get("username").strip(), get("password"), "" if kind == "other" else name)
            except Exception as e:
                return self.send(200, caldav_page(kind, csrf, short_error(e)))
            return self.redirect("/account?label=" + urllib.parse.quote(label))
        if url == "/connect/ics/save":
            label = get("label").strip()
            if not engine.NAME_OK.fullmatch(label):
                return self.send(200, ics_page(csrf, "Use letters, numbers, - and _ for the name."))
            link = engine.clean_ics_url(get("url"))
            now = engine.now_utc()
            engine.IcsCalendar(link).list(now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + engine.dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            engine._save_account(label, {"provider": "ics", "url": link})
            return self.redirect("/account?label=" + urllib.parse.quote(label))
        if url == "/account/remove":
            label = get("label")
            engine.update_config(lambda c: c.update(calendars=[x for x in c["calendars"] if x.get("account") != label]))
            engine.remove_account(label)
            return self.redirect("/connect?m=signedout")
        if url == "/calendar/add":
            name, account, cal_id = get("name").strip(), get("account"), get("calendar_id")
            if not engine.NAME_OK.fullmatch(name) or account not in engine._accounts():
                return self.send(200, account_page(account, csrf, "Use letters, numbers, - and _ for the name.") if account in engine._accounts() else connect_page(csrf))
            read_only = engine._accounts()[account]["provider"] == "ics"
            if not read_only:
                read_only = not next((c["writable"] for c in engine.account_calendars(account) if c["id"] == cal_id), False)
            if name in [c["name"] for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]]:
                return self.send(200, account_page(account, csrf, f"{name} is already in use. Pick another name."))
            engine.update_config(lambda c: c["calendars"].append({"name": name, "label": name, "account": account, "calendar_id": cal_id,
                                                                  "busy_only": bool(get("busy_only")), "read_only": read_only, "cannot_write": read_only, "skip_sources": []}))
            return self.redirect("/?m=added")
        if url == "/calendar/save":
            name = get("name")

            def save(c):
                others = {x["name"] for x in c["calendars"]}
                for x in c["calendars"]:
                    if x["name"] == name:
                        x.update(busy_only=bool(get("busy_only")), read_only=bool(get("read_only")) or x.get("cannot_write", False),
                                 skip_sources=[n for n in form.get("skip", []) if n in others and n != name])
            engine.update_config(save)
            return self.redirect("/?m=saved")
        if url == "/calendar/remove":
            name = get("name")
            engine.update_config(lambda c: c.update(calendars=[x for x in c["calendars"] if x["name"] != name]))
            return self.redirect("/?m=removed")
        if url == "/purge":
            cals = [c for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"] if c["name"] == get("name")]
            if not get("confirm"):
                count = engine.purge(cals, {c["name"]: engine.make_adapter(c) for c in cals}, dry_run=True, log=lambda *_: None)["deleted"]
                return self.send(200, page("Delete copies", f'''<h1>Delete {esc(count)} copies from {esc(get("name"))}?</h1><section><p>This removes only events calsync made. If syncing is still on, the next run makes them again, so stop syncing this calendar too if you're done with it.</p>
<form method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(get("name"))}"><input type="hidden" name="confirm" value="1"><div class="actions"><button class="danger">Delete {esc(count)} copies</button><a class="button plain" href="/">Keep them</a></div></form></section>''', True))
            s = engine.purge(cals, {c["name"]: engine.make_adapter(c) for c in cals}, log=lambda *_: None)
            return self.send(200, page("Copies deleted", f'<h1>Deleted {esc(s["deleted"])} copies</h1><section>{esc(s["errors"])} problems.</section><a class="button plain" href="/">Back</a>', True))
        if url == "/preview":
            lines = []
            s = engine.run_once(dry_run=True, log=lines.append)
            if s is None:
                return self.redirect("/?m=busy")
            with state_lock:
                last_preview[self.cookie()] = dict(s, lines=lines)
            return self.redirect("/")
        if url == "/start":
            engine.update_config(lambda c: c.update(enabled=True))
            run_in_background()
            return self.redirect("/?m=started")
        if url == "/pause":
            engine.update_config(lambda c: c.update(enabled=False))
            return self.redirect("/?m=paused")
        if url == "/run":
            return self.redirect("/?m=" + ("running" if run_in_background(force=bool(get("force"))) else "busy"))
        if url == "/settings/save":
            minutes, zone = get("interval"), get("timezone").strip()
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(zone)
            except Exception:
                return self.send(200, settings_page(csrf, "That time zone isn't known. Use a name like America/New_York."))
            if not minutes.isdigit() or int(minutes) not in INTERVALS:
                return self.send(200, settings_page(csrf, "Pick one of the listed intervals."))
            engine.update_config(lambda c: c.update(interval_minutes=int(minutes), timezone=zone))
            return self.redirect("/settings?m=saved")
        if url == "/settings/password":
            if not hmac.compare_digest(hash_password(get("old"), cfg["salt"]), cfg["hash"]):
                return self.send(200, settings_page(csrf, "The current password isn't right."))
            if len(get("new")) < 10:
                return self.send(200, settings_page(csrf, "Use at least 10 characters."))
            set_password(get("new"))
            fresh = web_config()
            return self.redirect("/settings?m=saved", [self.session_header(make_session(fresh), SESSION_DAYS * 86400)])
        self.send(404, page("Not found", "<h1>Page not found</h1>", True))

    # first run and sign-in

    def needs_code(self):
        """Only someone on this machine itself skips the setup code. A proxy on the same machine makes every visitor
        look local, so a public address, a forwarded request or a non-local host name all need the code. So does
        listening beyond loopback (the Docker image): port forwarders can make an outside visitor look local."""
        name = re.sub(r":\d+$", "", (self.headers.get("Host") or "").strip().lower()).strip("[]")
        local_name = name in ("localhost", "127.0.0.1", "::1") or name.endswith(".localhost")
        forwarded = any(self.headers.get(h) for h in PROXY_HEADERS)
        return not (is_loopback(BIND) and is_loopback(self.client_address[0]) and local_name and not forwarded and not PUBLIC_URL)

    def first_run_page(self, flash=None):
        code = ('<label for="code">Setup code</label><input type="text" id="code" name="code" required autocomplete="off">'
                '<p class="soft">It\'s in the logs: <code>docker compose logs calsync</code>. It proves you\'re the one who started this.</p>') if self.needs_code() else ""
        return page("Welcome", f'''<h1>Welcome to calsync</h1><section><p>Choose a password for this page. Your sign-ins stay on this machine, so this password is what guards them.</p>
<form method="post" action="/first-run" autocomplete="off">{code}<label for="pw">Password, at least 10 characters</label><input type="password" id="pw" name="password" required minlength="10"><div class="actions"><button>Continue</button></div></form></section>''', flash=flash)

    def first_run(self, get):
        if web_config():
            return self.redirect("/")
        ip = self.client_address[0]
        if self.needs_code():
            stamp = take_try("setup", ip)
            if stamp is None:
                return self.send(429, self.first_run_page("Too many tries. Wait a minute and try again."))
            if not hmac.compare_digest(get("code").strip().encode(), setup_code.encode()):
                return self.send(200, self.first_run_page("That setup code isn't right."))
            give_back("setup", ip, stamp)
        if len(get("password")) < 10:
            return self.send(200, self.first_run_page("Use at least 10 characters."))
        with first_run_lock:
            if web_config():
                return self.redirect("/")
            set_password(get("password"))
        return self.redirect("/", [self.session_header(make_session(web_config()), SESSION_DAYS * 86400)])

    def login(self, cfg, get):
        ip = self.client_address[0]
        stamp = take_try("login", ip)
        if stamp is None:
            return self.send(429, login_page("Too many tries. Wait a minute and try again."))
        if not hmac.compare_digest(hash_password(get("password"), cfg["salt"]), cfg["hash"]):
            return self.send(200, login_page("That password isn't right."))
        give_back("login", ip, stamp)
        return self.redirect("/", [self.session_header(make_session(cfg), SESSION_DAYS * 86400)])

    # Google: the browser comes back from Google to "/" with no session cookie guaranteed, so the one-use state proves it

    def google_redirect(self):
        if PUBLIC_URL:
            return PUBLIC_URL + "/"
        port = re.search(r":(\d+)$", self.headers.get("Host") or "")
        return f"http://127.0.0.1:{port.group(1) if port else 80}/"

    def google_start(self, label, csrf):
        if not engine.NAME_OK.fullmatch(label):
            return self.send(200, google_page(csrf, "Use letters, numbers, - and _ for the name."))
        client = engine.load_json(os.path.join(engine.SECRETS, "google-client.json"), None)
        if not client:
            return self.redirect("/connect/google")
        state, redirect = secrets.token_urlsafe(24), self.google_redirect()
        with state_lock:
            pending_google[state] = {"label": label, "redirect": redirect, "origin": "http://" + self.headers["Host"] if not PUBLIC_URL else PUBLIC_URL, "expires": time.time() + 900}
        link = engine.google_auth_url(client["client_id"], redirect, state)
        return self.send(200, page("Connect Google", f'''<h1>Sign in to Google</h1><section><ol><li><a class="button" href="{esc(link)}" target="_blank" rel="noreferrer noopener">Open Google sign-in</a></li>
<li>Google warns that the app isn't verified, because it's yours. Click Advanced, Go to calsync, then Allow.</li>
<li>If the browser ends on a page that won't load, copy that page's whole address and paste it here.</li></ol>
<form method="post" action="/connect/google/paste" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label for="addr">Address from the browser</label><input type="text" id="addr" name="address" required><div class="actions"><button class="plain">Finish sign-in</button></div></form></section>''', True))

    def google_callback(self, q):
        state = q.get("state", [""])[0]
        with state_lock:
            flow = pending_google.pop(state, None)
        if not flow:
            return self.send(400, page("Connect Google", '<h1>This sign-in link has expired</h1><section>Start again from Add a calendar.</section><a class="button plain" href="/connect">Add a calendar</a>'))
        if "code" not in q:
            return self.send(200, page("Connect Google", '<h1>Google didn\'t finish the sign-in</h1><section>Nothing changed.</section><a class="button plain" href="/connect">Back</a>'))
        problem = None
        try:
            problem = engine.google_finish(flow["label"], q["code"][0], flow["redirect"])
        except Exception as e:
            problem = short_error(e)
        if problem:
            return self.send(200, page("Connect Google", f'<h1>That didn\'t work</h1><section class="bad">{esc(problem)}</section><a class="button plain" href="{esc(flow["origin"])}/connect">Back</a>'))
        self.redirect(f"{flow['origin']}/account?label={urllib.parse.quote(flow['label'])}")

    def google_paste(self, address):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(address.strip()).query)
        return self.google_callback(q)

    def microsoft_wait(self, flow_id):
        with state_lock:
            flow = pending_ms.get(flow_id)
        if not flow:
            return self.send(200, page("Connect Microsoft", '<h1>This sign-in has expired</h1><a class="button plain" href="/connect/microsoft">Start again</a>', True))
        if flow["status"] == "done":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.redirect("/account?label=" + urllib.parse.quote(flow["label"]))
        if flow["status"] == "failed":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.send(200, page("Connect Microsoft", f'<h1>That didn\'t work</h1><section class="bad">{esc(flow.get("problem", ""))}</section><a class="button plain" href="/connect/microsoft">Try again</a>', True))
        return self.send(200, page("Connect Microsoft", f'''<h1>Sign in to Microsoft</h1><section><ol><li><a class="button" href="{esc(flow["link"])}" target="_blank" rel="noreferrer noopener">Open Microsoft sign-in</a></li><li>Enter this code:<p class="code">{esc(flow["user_code"])}</p></li><li>Sign in and accept. This page moves on by itself.</li></ol></section>''', True, refresh=5))


FLASHES = {"saved": "Saved.", "added": "Added. Preview to see what will change.", "removed": "Stopped syncing that calendar. Its copies on other calendars go away on the next run.",
           "busy": "A sync is already going. Try again in a moment.", "started": "Syncing started.", "paused": "Paused. Nothing changes until you start it again.",
           "running": "Sync started.", "signedout": "Signed out of that account."}


def login_page(flash=None):
    return page("Sign in", '<h1>Sign in</h1><section><form method="post" action="/login"><label for="pw">Password</label><input type="password" id="pw" name="password" required autofocus><div class="actions"><button>Sign in</button></div></form></section>', flash=flash)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve():
    engine.ensure_home()
    server = Server((BIND, PORT), Handler)

    def stop_now(*_):       # as a container's first process, nothing stops it on SIGTERM unless it asks to
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_now)
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(stop,), daemon=True).start()
    print(f"calsync is on port {PORT} (open http://localhost:{PORT})", flush=True)
    if not web_config():
        print(f"First-time setup code: {setup_code}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
