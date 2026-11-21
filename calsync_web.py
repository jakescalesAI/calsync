#!/usr/bin/env python3
"""The web setup page and background sync: `calsync web`, or the Docker image.

No JavaScript and no outside files, so a strict content policy holds. Everything a page shows goes
through esc(). Tokens, client secrets, CalDAV passwords and .ics links are stored by the engine and never sent to a page.

  CALSYNC_BIND        address to listen on (default 127.0.0.1; the Docker image uses 0.0.0.0)
  CALSYNC_PORT        port (default 8484)
  CALSYNC_PUBLIC_URL  https address, if you put it behind your own proxy; used as Google's redirect
  CALSYNC_HOSTS       extra host names allowed to reach it, comma separated
"""
import base64, hashlib, hmac, html, http.cookies, http.server, ipaddress, json, os, re, secrets, signal, threading, time, urllib.parse
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


def copies(n):
    return f"{esc(n)} {'copy' if n == 1 else 'copies'}"


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
    if not cfg or not re.fullmatch(r"[0-9]{1,12}", expires):     # anyone can send any cookie, so nothing here may raise
        return False
    return int(expires) > time.time() and hmac.compare_digest(mac.encode(), sign(cfg["key"], "session." + expires).encode())


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

# brand/mark-small-white.svg, sent inside the page so the content policy can stay closed; the stylesheet turns it dark-on-light for a light page
MARK = "data:image/svg+xml;base64," + base64.b64encode((
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="calsync">'
    '<rect x="2.85" y="4.85" width="58.3" height="58.3" rx="13.2" fill="#f5f5f5" stroke="#0a0a0a" stroke-width="1.7"/>'
    '<rect x="17.5" y="0" width="5" height="11.5" rx="2.5" fill="#f5f5f5" stroke="#0a0a0a" stroke-width="1.7"/>'
    '<rect x="41.5" y="0" width="5" height="11.5" rx="2.5" fill="#f5f5f5" stroke="#0a0a0a" stroke-width="1.7"/>'
    '<g fill="none" stroke="#0a0a0a" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 33V25.1A8.5 8.5 0 0 1 17.5 16.6H46.5A8.5 8.5 0 0 1 55 25.1V33.6M49.9 28.5L55 33.6L60.1 28.5"/><path transform="rotate(180 32 37.4)" d="M9 33V25.1A8.5 8.5 0 0 1 17.5 16.6H46.5A8.5 8.5 0 0 1 55 25.1V33.6M49.9 28.5L55 33.6L60.1 28.5"/></g>'
    '<path d="M25.78 50.67Q23.3 50.67 21.39 49.81Q19.47 48.96 18.37 47.46Q17.26 45.95 17.2 43.98L21.44 43.98Q21.5 44.94 22.08 45.65Q22.66 46.36 23.62 46.75Q24.58 47.13 25.78 47.13Q27.04 47.13 28.01 46.69Q28.98 46.25 29.54 45.45Q30.1 44.66 30.1 43.62Q30.1 42.54 29.53 41.72Q28.96 40.9 27.91 40.44Q26.85 39.97 25.35 39.97L23.27 39.97L23.27 36.72L25.35 36.72Q26.58 36.72 27.51 36.29Q28.44 35.86 28.96 35.08Q29.49 34.31 29.49 33.28Q29.49 32.29 29.04 31.55Q28.59 30.81 27.76 30.39Q26.93 29.97 25.83 29.97Q24.74 29.97 23.82 30.36Q22.91 30.75 22.34 31.46Q21.77 32.17 21.74 33.17L17.69 33.17Q17.72 31.2 18.82 29.71Q19.91 28.22 21.75 27.38Q23.59 26.53 25.86 26.53Q28.21 26.53 29.94 27.42Q31.68 28.3 32.63 29.78Q33.59 31.25 33.59 33.03Q33.59 35.02 32.42 36.36Q31.25 37.7 29.34 38.13L29.34 38.32Q30.98 38.54 32.12 39.29Q33.25 40.04 33.85 41.2Q34.44 42.37 34.44 43.84Q34.44 45.81 33.32 47.35Q32.2 48.9 30.25 49.78Q28.3 50.67 25.78 50.67ZM46.4 26.85L46.4 50.35L42.22 50.35L42.22 30.6L42.06 30.6L36.58 34.55L36.58 30.54L41.73 26.85Z" fill="#0a0a0a" stroke="#0a0a0a" stroke-width="0.5" stroke-linejoin="round"/>'
    '</svg>'
).encode()).decode()

CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--panel:#fff;--field:#fff;--well:#f0f0f0;--hover:rgba(0,0,0,.04);--ring:rgba(0,0,0,.08);--glass:rgba(250,250,250,.82);
--line:rgba(0,0,0,.1);--line2:rgba(0,0,0,.24);--edge:#858585;--text:#0a0a0a;--body:#353535;--dim:#5c5c5c;--faint:#737373;--bad:#b3261e;--bad-line:rgba(179,38,30,.4);--bad-bg:rgba(179,38,30,.05);
--sans:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#050505;--panel:#0c0c0c;--field:#050505;--well:#151515;--hover:rgba(255,255,255,.06);--ring:rgba(255,255,255,.08);--glass:rgba(5,5,5,.72);
--line:rgba(255,255,255,.09);--line2:rgba(255,255,255,.22);--edge:#6e6e6e;--text:#f5f5f5;--body:#c4c4c4;--dim:#9a9a9a;--faint:#808080;--bad:#ff9d9d;--bad-line:rgba(255,157,157,.45);--bad-bg:rgba(255,120,120,.07)}}
*,*::before,*::after{box-sizing:border-box}html{-webkit-text-size-adjust:100%;text-size-adjust:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--body);font:400 16px/1.55 var(--sans);-webkit-font-smoothing:antialiased;overflow-wrap:break-word}
a{color:var(--text);text-decoration:underline;text-decoration-color:var(--line2);text-decoration-thickness:1px;text-underline-offset:.2em}a:hover{text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--text);outline-offset:2px}b,strong{color:var(--text);font-weight:600}p{margin:0 0 12px}.soft{color:var(--dim)}
header{position:sticky;top:0;z-index:5;background:var(--glass);border-bottom:1px solid var(--line);-webkit-backdrop-filter:saturate(140%) blur(14px);backdrop-filter:saturate(140%) blur(14px)}
.bar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 24px;max-width:1040px;min-height:64px;margin:0 auto;padding:10px 24px}
.brand{display:inline-flex;align-items:center;gap:10px;font-weight:600;font-size:19px;letter-spacing:-.015em;text-decoration:none}.brand img,.empty img{display:block}
nav{display:flex;gap:4px;font-size:14px}nav a{padding:7px 13px;border-radius:999px;color:var(--dim);font-weight:500;text-decoration:none;white-space:nowrap}
nav a:hover{color:var(--text);background:var(--hover)}nav a[aria-current]{color:var(--text);background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
main{max-width:720px;margin:0 auto;padding:40px 24px 96px}main.wide{max-width:1040px}
h1,h2{color:var(--text);font-weight:600;text-wrap:balance;overflow-wrap:anywhere}h1{font-size:30px;line-height:1.1;letter-spacing:-.035em;margin:0 0 20px}
h2{font-size:18px;line-height:1.3;letter-spacing:-.02em;margin:40px 0 12px}h1+h2{margin-top:28px}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:0 0 12px}section>:first-child{margin-top:0}section>:last-child,section>form>:last-child{margin-bottom:0}
label{display:block;margin:16px 0 6px;color:var(--text);font-size:14px;font-weight:600}form>label:first-child,input[type=hidden]+label{margin-top:0}
label.check{display:flex;gap:10px;align-items:flex-start;margin:10px 0;color:var(--body);font-size:15px;font-weight:400;cursor:pointer}
input[type=checkbox]{flex:none;width:18px;height:18px;margin:2px 0 0;accent-color:var(--text);cursor:pointer}
input[type=text],input[type=password],input[type=url],select{display:block;width:100%;min-height:44px;padding:9px 12px;border:1px solid var(--edge);border-radius:10px;background:var(--field);color:var(--text);font:inherit}
input::placeholder{color:var(--faint);opacity:1}input+p.soft{margin:6px 0 0;font-size:14px}input:hover,select:hover{border-color:var(--text)}input:focus-visible,select:focus-visible{outline-offset:1px;border-color:var(--text)}
button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:9px 18px;border:1px solid var(--text);border-radius:999px;background:var(--text);color:var(--bg);
font:600 15px/1.25 var(--sans);text-align:center;text-decoration:none;cursor:pointer;transition:background-color .15s,border-color .15s,color .15s,box-shadow .15s}
button:hover,.button:hover{box-shadow:0 0 0 4px var(--ring)}button.plain,.button.plain{background:transparent;color:var(--text);border-color:var(--line2)}
button.plain:hover,.button.plain:hover{background:var(--hover);border-color:var(--text);box-shadow:none}button.danger{background:transparent;color:var(--bad);border-color:var(--bad-line)}
button.danger:hover{background:var(--bad);color:var(--bg);border-color:var(--bad);box-shadow:none}form.inline{display:inline-flex}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:20px}.actions.flat{margin:0}
.flash{display:flex;align-items:baseline;gap:10px;margin:0 0 20px;padding:12px 16px;border:1px solid var(--line2);border-radius:12px;background:var(--well);color:var(--text);font-weight:500}
.flash::before,.state::before{content:"";flex:none;width:8px;height:8px;border-radius:50%;background:var(--text);transform:translateY(-1px)}
.bad{color:var(--bad);font-weight:600}section.bad{border-color:var(--bad-line);background:var(--bad-bg);font-weight:500;overflow-wrap:anywhere}
.note{border-style:dashed;border-color:var(--line2);background:transparent;color:var(--dim);font-size:15px}
.empty{display:grid;justify-items:center;gap:8px;padding:56px 24px;border-style:dashed;border-color:var(--line2);text-align:center}.empty p{max-width:34ch;margin:8px 0}
.list{padding:0}.row{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;padding:14px 20px;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.row>div:not(.actions){flex:1 1 150px;min-width:0;overflow-wrap:anywhere}.row .soft{display:block;font-size:14px}.row button,.row .button{min-height:36px;padding:6px 15px;font-size:14px}
.tile{flex:none;display:grid;place-items:center;width:34px;height:34px;border:1px solid var(--line);border-radius:9px;background:var(--well);color:var(--text);font-weight:600;font-size:14px}
.head{display:flex;align-items:center;gap:16px;margin:0 0 16px}section+.button,h1+.button{margin-top:8px}.out main>.button{display:flex;width:fit-content;margin:16px auto 0}
.chip{display:inline-block;padding:2px 9px;border:1px solid var(--line2);border-radius:999px;color:var(--dim);font-size:12px;line-height:1.5;white-space:nowrap}
.cal{padding:0}summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:14px 20px;border-radius:13px;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary:hover{background:var(--hover)}.who{flex:1 1 130px;display:grid;min-width:0;overflow-wrap:anywhere}.who .soft{font-size:13px}.chips{display:flex;flex-wrap:wrap;gap:6px}
.more{color:var(--dim);font-size:13px;white-space:nowrap}.more::after{content:"";display:inline-block;width:7px;height:7px;margin-left:8px;border:solid;border-width:0 1.5px 1.5px 0;transform:translateY(-3px) rotate(45deg);transition:transform .15s}
details[open] .more{color:var(--text)}details[open] .more::after{transform:translateY(1px) rotate(225deg)}details[open]>summary{border-radius:13px 13px 0 0}
.opts{padding:16px 20px 20px;border-top:1px solid var(--line)}.opts>.actions{padding-top:16px;border-top:1px solid var(--line)}
.state{display:flex;align-items:baseline;gap:10px;color:var(--text);font-weight:600}.state::before{background:transparent;box-shadow:inset 0 0 0 1.5px var(--faint)}.state.on::before{background:var(--text);box-shadow:0 0 0 4px var(--ring)}
.state+.soft{font-size:14px}.tally{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 14px;padding:0;list-style:none}
.tally li{margin:0;padding:10px 12px;border-radius:10px;background:var(--well);color:var(--text);font-weight:600;font-size:15px}
ol{margin:16px 0;padding:0;list-style:none;counter-reset:step}ol li{position:relative;min-height:28px;margin:0;padding:11px 0 11px 44px;border-top:1px solid var(--line);counter-increment:step}
ol li:first-child{border-top:0}ol li::before{content:counter(step);position:absolute;left:0;top:10px;display:grid;place-items:center;width:28px;height:28px;border:1px solid var(--line2);border-radius:50%;color:var(--text);font:600 13px var(--mono)}
ol+form{padding-top:20px;border-top:1px solid var(--line)}code,pre,.code{font-family:var(--mono)}code{padding:.1em .4em;border:1px solid var(--line);border-radius:6px;background:var(--well);color:var(--text);font-size:.86em;overflow-wrap:anywhere}
pre{margin:12px 0;padding:14px 16px;max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--well);color:var(--text);font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}
ol li>.button:first-child{margin:-7px 0}.code{width:fit-content;max-width:100%;margin:10px 0 0;padding:12px 18px;border:1px dashed var(--line2);border-radius:10px;color:var(--text);font-weight:600;font-size:28px;line-height:1.2;letter-spacing:.12em;user-select:all}
.out header{position:static;border:0;background:none;-webkit-backdrop-filter:none;backdrop-filter:none}.out .bar{justify-content:center;padding-top:56px}.out .brand{font-size:22px}
.out main{max-width:440px;padding-top:20px}.out h1{font-size:26px;text-align:center}.out form button{width:100%}.out .actions{justify-content:center}
@media (prefers-color-scheme:light){.brand img,.empty img{filter:invert(1)}}
@media (min-width:900px){.cols{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:0 32px;align-items:start}.cols>div>h2:first-child{margin:0 0 20px;line-height:33px}}
@media (max-width:560px){header{position:static}.bar{padding:10px 16px}nav{flex:1 1 100%}nav a{flex:1;padding:7px 4px;text-align:center}main{padding:28px 16px 72px}h1{font-size:26px}
section{padding:16px}.list,.cal{padding:0}.row,summary{padding:14px 16px}.opts{padding:16px}.chips{order:1;flex-basis:100%;padding-left:46px}.row.long>div{flex-basis:100%}.tally li{padding:10px;font-size:14px}.out .bar{padding-top:36px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

NAV = (("/", "Sync"), ("/connect", "Add a calendar"), ("/settings", "Settings"))


def page(title, body, signed_in=False, flash=None, refresh=None, at="", wide=False):
    """at is the header link this page belongs under; wide gives the two-column sync page room."""
    links = "".join('<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if href == at else "", name) for href, name in NAV)
    nav = f'<nav aria-label="Main">{links}</nav>' if signed_in else ""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    note = f'<p class="flash" role="status">{esc(flash)}</p>' if flash else ""
    size = 30 if signed_in else 40
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">{meta}'
            f'<title>{esc(title)} · calsync</title><link rel="stylesheet" href="/app.css"><link rel="icon" href="{MARK}">'
            f'<body class="{"in" if signed_in else "out"}"><header><div class="bar"><a class="brand" href="/"><img src="{MARK}" alt="" width="{size}" height="{size}">calsync</a>{nav}</div></header>'
            f'<main{" class=wide" if wide else ""}>{note}{body}</main></html>')


def tile(name):
    """The square with a calendar's or an account's first letter, as on getcalsync.app."""
    return f'<span class="tile" aria-hidden="true">{esc(str(name)[:1].upper())}</span>'


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
    if not accounts:
        return page("Sync", f'<h1>Your calendars</h1><section class="empty"><img src="{MARK}" alt="" width="44" height="44"><p>Nothing is connected yet. Start by adding your first calendar.</p>'
                    '<a class="button" href="/connect">Add a calendar</a></section>', True, flash, at="/")
    out = ['<div class="cols"><div><h1>Your calendars</h1>']     # calendars on the left, sync and its preview on the right; one column on a phone
    names = [c["name"] for c in cals]
    for c in cals:
        flags = [f for f, on in (("Busy only", c.get("busy_only")), ("Never written to", c.get("read_only"))) if on]
        flags += [f"Skips {x}" for x in c.get("skip_sources", [])]
        chips = "".join(f'<span class="chip">{esc(f)}</span>' for f in flags)
        skip = "".join(f'<label class="check"><input type="checkbox" name="skip" value="{esc(n)}"{" checked" if n in c.get("skip_sources", []) else ""}> Don\'t copy {esc(n)} here</label>'
                       for n in names if n != c["name"])
        out.append(f'''<section class="cal"><details><summary>{tile(c.get("label") or c["name"])}<span class="who"><strong>{esc(c.get("label") or c["name"])}</strong><span class="soft">{esc(c["account"])}</span></span>
<span class="chips">{chips}</span><span class="more">Options</span></summary><div class="opts"><form method="post" action="/calendar/save"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}">
<label class="check"><input type="checkbox" name="busy_only"{" checked" if c.get("busy_only") else ""}> Copies here only say Busy</label>
<label class="check"><input type="checkbox" name="read_only"{" checked" if c.get("read_only") else ""}> Never write to this calendar</label>
{skip}<div class="actions"><button>Save</button></div></form>
<div class="actions"><form class="inline" method="post" action="/calendar/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="plain">Stop syncing this calendar</button></form>
<form class="inline" method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="danger">Delete the copies calsync made here</button></form></div></div></details></section>''')
    if len(cals) < 2:
        out.append('<section class="note">Add at least two calendars and calsync can start.</section>')
    out.append('<div class="actions"><a class="button plain" href="/connect">Add a calendar</a></div></div><div><h2>Sync</h2>')
    state = "Running in the background every " + str(cfg.get("interval_minutes", 5)) + " minutes." if cfg.get("enabled") else "Not running yet. Preview first, then start it."
    out.append(f'<section><p class="state{" on" if cfg.get("enabled") else ""}">{esc(state)}</p><p class="soft">Last run: {esc(describe_run(last))}</p>')
    if last and last.get("problems"):
        out.append('<p class="bad">Last run had problems:</p><pre tabindex="0">' + esc("\n".join(last["problems"])) + "</pre>")
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
        out.append(f'<h2>Preview</h2><section><ul class="tally"><li>{esc(preview["created"])} to add</li><li>{esc(preview["updated"])} to update</li><li>{esc(preview["deleted"])} to remove</li></ul><p>Nothing has changed yet.</p>')
        if preview.get("skipped_deletes"):
            out.append(f'<p class="bad">{esc(preview["skipped_deletes"])} deletes would be held back by the safety stop.</p>')
        out.append('<pre tabindex="0">' + esc("\n".join(preview["lines"][:400]) or "Nothing to change.") + "</pre>")
        hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
        out.append(f'<div class="actions"><form class="inline" method="post" action="/start">{hidden}<button>Apply and keep syncing</button></form>')
        if preview.get("skipped_deletes"):
            out.append(f'<form class="inline" method="post" action="/run">{hidden}<input type="hidden" name="force" value="1"><button class="danger">Allow these deletes</button></form>')
        out.append("</div></section>")
    return page("Sync", "".join(out) + "</div></div>", True, flash, at="/", wide=True)


def connect_page(csrf, flash=None):
    accounts = engine._accounts()
    rows = []
    for label, a in accounts.items():
        kind = a.get("service") or {"google": "Google", "outlook": "Microsoft", "caldav": "CalDAV", "ics": ".ics link"}.get(a["provider"], "")
        rows.append(f'''<div class="row">{tile(label)}<div><b>{esc(label)}</b> <span class="soft">{kind}</span></div><div class="actions flat"><a class="button" href="/account?label={urllib.parse.quote(label)}">Choose calendars</a>
<form class="inline" method="post" action="/account/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="label" value="{esc(label)}"><button class="plain">Sign out</button></form></div></div>''')
    existing = ('<h2>Signed in</h2><section class="list">' + "".join(rows) + "</section>") if rows else ""
    caldav = "".join(f'''<div class="row"><div><b>{"Another CalDAV server" if k == "other" else v[0]}</b></div><a class="button" href="/connect/caldav?p={k}">Connect</a></div>'''
                     for k, v in engine.CALDAV_SERVERS.items())
    return page("Add a calendar", f'''<h1>Add a calendar</h1>{existing}<h2>Connect an account</h2>
<section class="list"><div class="row"><div><b>Google Calendar</b></div><a class="button" href="/connect/google">Connect</a></div>
<div class="row"><div><b>Outlook or Microsoft 365</b></div><a class="button" href="/connect/microsoft">Connect</a></div>
{caldav}</section>
<section class="list"><div class="row long"><div><b>Published .ics link</b> <span class="soft">Read-only: its events are copied out, nothing is written back. For Proton Calendar, an Exchange server, or an organization that blocks outside apps.</span></div><a class="button" href="/connect/ics">Add link</a></div>
<div class="row long"><div><b>Calendly, Cal.com or another booking app</b> <span class="soft">Nothing to add here. Connect the booking app to any calendar above. calsync keeps that calendar showing your busy times from all the others.</span></div><a class="button plain" href="https://docs.getcalsync.app/booking-apps/">How it works</a></div></section>''', True, flash, at="/connect")


def google_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "google-client.json"), None)
    if client:
        return page("Connect Google", f'''<h1>Connect Google</h1><section><form method="post" action="/connect/google/start"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="work">
<div class="actions"><button>Continue</button></div></form></section>
<section><p>Your Google sign-in app is saved. Replace it only if you made a new one.</p>{client_form(csrf, True)}</section>''', True, flash, at="/connect")
    return page("Connect Google", f'<h1>Connect Google</h1><section><p>Google needs a sign-in app of your own. It\'s free and takes about 10 minutes. You only do it once, for all your Google accounts.</p>{GOOGLE_STEPS}{client_form(csrf)}</section>', True, flash, at="/connect")


def client_form(csrf, replacing=False):
    """Replacing a saved app is the side road, Continue is the way on, so its Save is the quieter button."""
    return f'''<form method="post" action="/connect/google/client" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Client ID</label><input type="text" id="cid" name="client_id" required><label for="sec">Client secret</label><input type="password" id="sec" name="client_secret" required>
<div class="actions"><button{" class=plain" if replacing else ""}>Save</button></div></form>'''


def microsoft_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "microsoft-client.json"), None)
    steps = "" if client else f"<p>Microsoft needs a sign-in app of your own. It's free and you only make it once.</p>{MICROSOFT_STEPS}"
    cid = esc(client["client_id"]) if client else ""
    return page("Connect Microsoft", f'''<h1>Connect Outlook or Microsoft 365</h1><section>{steps}<form method="post" action="/connect/microsoft/start" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Application (client) ID</label><input type="text" id="cid" name="client_id" required value="{cid}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="school">
<div class="actions"><button>Continue</button></div></form></section>''', True, flash, at="/connect")


def ics_page(csrf, flash=None):
    return page("Add an .ics link", f'''<h1>Add a published .ics link</h1><section><p>calsync saves this link and never shows it again, since anyone who has it can read that calendar.</p>
<form method="post" action="/connect/ics/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="classes">
<label for="url">Calendar link</label><input type="password" id="url" name="url" required>
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash, at="/connect")


def caldav_page(kind, csrf, flash=None):
    name, server, note = engine.CALDAV_SERVERS[kind]
    address = "" if server else '<label for="server">Server address</label><input type="text" id="server" name="server" required placeholder="https://cloud.example.com">'
    return page(f"Connect {name}", f'''<h1>Connect {name}</h1><section><p>{note}</p><p>calsync saves this password on this machine and never shows it again.</p>
<form method="post" action="/connect/caldav/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="kind" value="{kind}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="personal">
{address}<label for="username">Username</label><input type="text" id="username" name="username" required>
<label for="password">Password</label><input type="password" id="password" name="password" required autocomplete="new-password">
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash, at="/connect")


def account_page(label, csrf, flash=None):
    try:
        found = engine.account_calendars(label)
    except Exception as e:
        return page("Choose calendars", f'<h1>{esc(label)}</h1><section class="bad">Couldn\'t read this account\'s calendars: {esc(short_error(e))}</section>', True, flash, at="/connect")
    used = {(c.get("account"), c.get("calendar_id")) for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
    rows = []
    for i, c in enumerate(found):
        # a Google calendar is often named after its address, and a name goes to anything that shows your calendars
        part = "main" if "@" in c["name"] else c["name"]
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{label.split('@')[0]}-{part}" if len(found) > 1 else label.split("@")[0]).strip("-_")[:40] or f"calendar-{i + 1}"
        if (label, c["id"]) in used:
            rows.append(f'<section class="list"><div class="row">{tile(c["name"])}<div><b>{esc(c["name"])}</b></div><span class="chip">already syncing</span></div></section>')
            continue
        ro = "" if c["writable"] else '<p class="soft">calsync can read this one but not write to it.</p>'
        rows.append(f'''<section><p class="head">{tile(c["name"])}<b>{esc(c["name"])}</b></p>{ro}<form method="post" action="/calendar/add"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="account" value="{esc(label)}"><input type="hidden" name="calendar_id" value="{esc(c["id"])}">
<label for="n{i}">Name in calsync</label><input type="text" id="n{i}" name="name" value="{esc(slug)}" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*">
{'<label class="check"><input type="checkbox" name="busy_only"> Copies here only say Busy</label>' if c["writable"] else ""}
<div class="actions"><button>Sync this calendar</button></div></form></section>''')
    listed = "".join(rows) or '<section class="note">This account has no calendars.</section>'
    return page("Choose calendars", f'<h1>{esc(label)}: choose calendars</h1>{listed}<div class="actions"><a class="button plain" href="/">Back to sync</a></div>', True, flash, at="/connect")


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
<form class="inline" method="post" action="/logout/everywhere"><input type="hidden" name="csrf" value="{csrf}"><button class="plain">Sign out on every device</button></form></div>''', True, flash, at="/settings")


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
    timeout = 30        # a connection that goes quiet is dropped, so it can't hold a thread forever

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
        # same-origin, not no-referrer: under no-referrer a browser sends "Origin: null" with this page's own
        # form posts, and same_origin() rightly refuses that, so every form was blocked.
        self.send_header("Referrer-Policy", "same-origin")
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
        if not hmac.compare_digest(get("csrf").encode(), self.csrf().encode()):
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
                return self.send(200, page("Delete copies", f'''<h1>Delete {copies(count)} from {esc(get("name"))}?</h1><section><p>This removes only events calsync made. If syncing is still on, the next run makes them again, so stop syncing this calendar too if you're done with it.</p>
<form method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(get("name"))}"><input type="hidden" name="confirm" value="1"><div class="actions"><button class="danger">Delete {copies(count)}</button><a class="button plain" href="/">Keep them</a></div></form></section>''', True, at="/"))
            s = engine.purge(cals, {c["name"]: engine.make_adapter(c) for c in cals}, log=lambda *_: None)
            return self.send(200, page("Copies deleted", f'<h1>Deleted {copies(s["deleted"])}</h1><section>{esc(s["errors"])} problems.</section><a class="button plain" href="/">Back</a>', True, at="/"))
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
            ip = self.client_address[0]
            stamp = take_try("login", ip)    # a borrowed session shouldn't get to guess the password here
            if stamp is None:
                return self.send(429, settings_page(csrf, "Too many tries. Wait a minute and try again."))
            if not hmac.compare_digest(hash_password(get("old"), cfg["salt"]), cfg["hash"]):
                return self.send(200, settings_page(csrf, "The current password isn't right."))
            give_back("login", ip, stamp)
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
<form method="post" action="/connect/google/paste" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label for="addr">Address from the browser</label><input type="text" id="addr" name="address" required><div class="actions"><button class="plain">Finish sign-in</button></div></form></section>''', True, at="/connect"))

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
            return self.send(200, page("Connect Microsoft", '<h1>This sign-in has expired</h1><a class="button plain" href="/connect/microsoft">Start again</a>', True, at="/connect"))
        if flow["status"] == "done":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.redirect("/account?label=" + urllib.parse.quote(flow["label"]))
        if flow["status"] == "failed":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.send(200, page("Connect Microsoft", f'<h1>That didn\'t work</h1><section class="bad">{esc(flow.get("problem", ""))}</section><a class="button plain" href="/connect/microsoft">Try again</a>', True, at="/connect"))
        return self.send(200, page("Connect Microsoft", f'''<h1>Sign in to Microsoft</h1><section><ol><li><a class="button" href="{esc(flow["link"])}" target="_blank" rel="noreferrer noopener">Open Microsoft sign-in</a></li><li>Enter this code:<p class="code">{esc(flow["user_code"])}</p></li><li>Sign in and accept. This page moves on by itself.</li></ol></section>''', True, refresh=5, at="/connect"))


FLASHES = {"saved": "Saved.", "added": "Added. Preview to see what will change.", "removed": "Stopped syncing that calendar. Its copies on other calendars go away on the next run.",
           "busy": "A sync is already going. Try again in a moment.", "started": "Syncing started.", "paused": "Paused. Nothing changes until you start it again.",
           "running": "Sync started.", "signedout": "Signed out of that account."}


def login_page(flash=None):
    return page("Sign in", '<h1>Sign in</h1><section><form method="post" action="/login"><label for="pw">Password</label><input type="password" id="pw" name="password" required autofocus><div class="actions"><button>Sign in</button></div></form></section>', flash=flash)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    MAX_CONNECTIONS = 64    # one person uses this page, so more at once than this is someone leaning on it

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if self.slots.acquire(blocking=False):
            super().process_request(request, client_address)
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


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
