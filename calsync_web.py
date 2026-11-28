#!/usr/bin/env python3
"""The web setup page and background sync: `calsync web`, or the Docker image.

No outside files and no inline scripts or styles, so a strict content policy holds. Every page works with plain links
and forms; one same-origin script (/app.js) adds keyboard keys, dragging events and live previews. Everything a page
shows goes through esc(). Tokens, client secrets, CalDAV passwords and .ics links are stored by the engine and never
sent to a page.

Start it from code with serve(host, port, home), for the desktop app or a hosted copy.

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
EVERYONE_PER_MINUTE = {"login": 30, "setup": 10, "recover": 10}   # and from all addresses together, so many addresses don't help
RECOVERY_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L: a code someone copies by hand
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")

state_lock = threading.Lock()
pending_google = {}     # state -> label, redirect, origin, expires
pending_ms = {}         # flow id -> label, tenant, device_code, user_code, link, expires, status
pending_oauth = {}      # one-click sign-in: state -> provider, verifier, redirect, expires, status, label, problem
here = threading.local()     # the request being answered: its Host header, so a page can build a loopback address
MAX_OAUTH_FLOWS = 40
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
    """A new key signs everyone out. A recovery code already made stays valid."""
    salt = secrets.token_hex(16)
    engine.ensure_home()
    old = web_config() or {}
    data = {"salt": salt, "hash": hash_password(password, salt), "key": secrets.token_hex(32)}
    if old.get("recovery"):
        data["recovery"] = old["recovery"]
    engine.save_json(web_file(), data)


def new_recovery_code():
    """Make a recovery code, keep only its hash, and return it so it can be shown once. It replaces any older code.
    16 letters from 31 is about 79 bits, and it only works from this machine, a few tries a minute."""
    code = "-".join("".join(secrets.choice(RECOVERY_LETTERS) for _ in range(4)) for _ in range(4))
    salt = secrets.token_hex(16)
    cfg = web_config() or {}
    cfg["recovery"] = {"salt": salt, "hash": hash_password(clean_code(code), salt), "made": engine.now_utc().strftime("%Y-%m-%d")}
    engine.save_json(web_file(), cfg)
    return code


def clean_code(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def recovery_ok(cfg, code):
    r = (cfg or {}).get("recovery")
    return bool(r) and hmac.compare_digest(hash_password(clean_code(code), r["salt"]), r["hash"])


def reset_password_here(argv=()):
    """`calsync reset-password`: on the machine calsync runs on, set a new page password without the old one.
    Whoever can run this can already read calsync's folder, so it gives nothing away. It signs everyone out
    and makes a new recovery code."""
    import getpass
    new = getpass.getpass("New password for the CalSync page (at least 10 characters, hidden): ")
    if len(new) < 10:
        raise SystemExit("Use at least 10 characters. Nothing changed.")
    if getpass.getpass("Type it again: ") != new:
        raise SystemExit("Those didn't match. Nothing changed.")
    set_password(new)
    code = new_recovery_code()
    print("Password changed. Every browser that was signed in is signed out.")
    print(f"New recovery code: {code}")
    print("Keep it somewhere safe. It resets the password from this computer if you forget it again.")
    return 0


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

# brand/mark.svg (made by tools/build_icons.py; mark-small.svg is the same artwork), sent inside the page so the content policy can stay closed
MARK = "data:image/svg+xml;base64," + base64.b64encode((
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="CalSync"><defs><linearGradient id="t" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="512"><stop offset="0" stop-color="#2c3648"/><stop offset="1" stop-color="#0b0f16"/></linearGradient><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="0" y1="104" x2="0" y2="394"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#d9e1ec"/></linearGradient></defs><rect width="512" height="512" rx="114" fill="url(#t)"/><rect x="120" y="150" width="272" height="244" rx="56" fill="url(#g)"/><rect x="153" y="96" width="62" height="108" rx="31" fill="url(#t)"/><rect x="165" y="108" width="38" height="84" rx="19" fill="url(#g)"/><rect x="297" y="96" width="62" height="108" rx="31" fill="url(#t)"/><rect x="309" y="108" width="38" height="84" rx="19" fill="url(#g)"/><rect x="120" y="212" width="272" height="10" fill="url(#t)"/><g fill="url(#t)" stroke="url(#t)"><path d="M201.5 288.2A58 58 0 0 1 303.2 274.3" fill="none" stroke-width="23" stroke-linecap="round"/><path d="M315.3 301.6L327.2 269.8L284.6 287.1Z" stroke-width="8" stroke-linejoin="round"/><path d="M310.5 327.8A58 58 0 0 1 208.8 341.7" fill="none" stroke-width="23" stroke-linecap="round"/><path d="M196.7 314.4L184.8 346.2L227.4 328.9Z" stroke-width="8" stroke-linejoin="round"/></g></svg>'
).encode()).decode()

CSS = """
:root{color-scheme:light;--bg:#fff;--panel:#f5f5f7;--field:#fff;--well:#fff;--hover:rgba(0,0,0,.04);--ring:rgba(0,113,227,.2);--glass:rgba(255,255,255,.8);
--line:rgba(0,0,0,.08);--text:#1d1d1f;--body:#1d1d1f;--dim:#6e6e73;--faint:#86868b;--blue:#0071e3;--tint:rgba(0,113,227,.1);--green:#34c759;--thumb:#fff;--thumb-shadow:0 1px 3px rgba(0,0,0,.12),0 0 0 .5px rgba(0,0,0,.04);
--bad:#e3342f;--bad-bg:rgba(227,52,47,.08);--ease:cubic-bezier(.32,.72,0,1);
--sans:-apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,"Segoe UI",system-ui,sans-serif;--display:-apple-system,BlinkMacSystemFont,"SF Pro Display",Inter,"Segoe UI",system-ui,sans-serif;--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace}
*,*::before,*::after{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html{-webkit-text-size-adjust:100%;text-size-adjust:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--body);font:400 17px/1.35 var(--sans);letter-spacing:-.022em;-webkit-font-smoothing:antialiased;overflow-wrap:break-word}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:.2em}
:focus-visible{outline:3px solid var(--blue);outline-offset:2px}b,strong{color:var(--text);font-weight:600}p{margin:0 0 12px}.soft{color:var(--dim)}
header{position:sticky;top:0;z-index:5;background:var(--glass);box-shadow:0 .5px 0 var(--line);-webkit-backdrop-filter:saturate(180%) blur(24px);backdrop-filter:saturate(180%) blur(24px)}
.bar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 24px;max-width:1040px;min-height:60px;margin:0 auto;padding:8px 24px}
.brand{display:inline-flex;align-items:center;gap:10px;color:var(--text);font:600 20px/1 var(--display);letter-spacing:-.03em;text-decoration:none}.brand:hover{text-decoration:none}.brand img,.empty img{display:block}
nav{display:flex;gap:2px;padding:3px;border-radius:10px;background:var(--panel);font-size:13px}nav a{padding:6px 14px;border-radius:7px;color:var(--text);font-weight:500;text-decoration:none;white-space:nowrap;transition:background-color .25s var(--ease)}
nav a:hover{text-decoration:none}nav a[aria-current]{background:var(--thumb);font-weight:600;box-shadow:var(--thumb-shadow)}
main{max-width:720px;margin:0 auto;padding:36px 24px 96px}main.wide{max-width:1040px}
h1,h2{color:var(--text);text-wrap:balance;overflow-wrap:anywhere}h1{font:700 36px/1.1 var(--display);letter-spacing:-.035em;margin:0 0 20px}
h2{margin:32px 0 8px;padding:0 20px;color:var(--dim);font-size:13px;font-weight:400;line-height:1.3;letter-spacing:0;text-transform:uppercase}h1+h2{margin-top:24px}
section{background:var(--panel);border-radius:18px;padding:22px;margin:0 0 12px}section>:first-child{margin-top:0}section>:last-child,section>form>:last-child{margin-bottom:0}
label{display:block;margin:16px 0 6px;color:var(--text);font-size:15px;font-weight:600}form>label:first-child,input[type=hidden]+label{margin-top:0}
label.check{display:flex;gap:12px;align-items:flex-start;margin:10px 0;color:var(--body);font-size:17px;font-weight:400;cursor:pointer}
input[type=checkbox]{flex:none;width:20px;height:20px;margin:1px 0 0;accent-color:var(--blue);cursor:pointer}
input[type=text],input[type=password],input[type=url],select{display:block;width:100%;min-height:44px;padding:10px 14px;border:1px solid rgba(0,0,0,.12);border-radius:10px;background:var(--field);color:var(--text);font:inherit}
input::placeholder{color:var(--faint);opacity:1}input+p.soft{margin:6px 0 0;font-size:13px}input:focus-visible,select:focus-visible{outline:0;box-shadow:0 0 0 4px var(--ring)}
button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 20px;border:0;border-radius:999px;background:var(--blue);color:#fff;
font:600 17px/1.2 var(--sans);letter-spacing:-.022em;text-align:center;text-decoration:none;cursor:pointer;transition:opacity .2s var(--ease),background-color .2s var(--ease),color .2s var(--ease)}
button:hover,.button:hover{text-decoration:none}button:active,.button:active{opacity:.35}
button.plain,.button.plain{background:var(--tint);color:var(--blue)}button.danger{background:var(--bad-bg);color:var(--bad)}form.inline{display:inline-flex}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:20px}.actions.flat{margin:0}
.flash{display:flex;align-items:center;gap:10px;width:fit-content;max-width:100%;margin:0 auto 20px;padding:10px 18px;border-radius:999px;background:var(--panel);color:var(--text);font-size:15px;font-weight:600;box-shadow:0 0 0 .5px var(--line),0 8px 24px rgba(0,0,0,.08)}
.flash::before,.state::before{content:"";flex:none;width:8px;height:8px;border-radius:50%;background:var(--green)}
.bad{color:var(--bad);font-weight:600}section.bad{background:var(--bad-bg);font-weight:500;overflow-wrap:anywhere}
.note{background:transparent;box-shadow:inset 0 0 0 .5px var(--line);color:var(--dim);font-size:15px}
.empty{display:grid;justify-items:center;gap:8px;padding:56px 24px;text-align:center}.empty p{max-width:34ch;margin:8px 0;color:var(--dim)}
.list{padding:0}.row{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;padding:12px 20px}.row+.row::before{content:"";position:absolute;top:0;left:72px;right:0;border-top:.5px solid var(--line)}
.row>div:not(.actions){flex:1 1 150px;min-width:0;overflow-wrap:anywhere}.row .soft{display:block;font-size:15px}.row button,.row .button{min-height:34px;padding:6px 15px;font-size:15px}
.tile{flex:none;display:grid;place-items:center;width:36px;height:36px;border-radius:9px;background:linear-gradient(#2c3648,#0b0f16);color:#fff;font:600 16px/1 var(--display)}
.head{display:flex;align-items:center;gap:16px;margin:0 0 16px}section+.button,h1+.button{margin-top:8px}.out main>.button{display:flex;width:fit-content;margin:16px auto 0}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;background:#fff;box-shadow:inset 0 0 0 .5px var(--line);color:var(--dim);font-size:12px;font-weight:500;line-height:1.5;white-space:nowrap}
.cal{padding:0}summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:12px 20px;border-radius:14px;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary:hover{background:var(--hover)}.who{flex:1 1 130px;display:grid;min-width:0;overflow-wrap:anywhere}.who .soft{font-size:15px}.chips{display:flex;flex-wrap:wrap;gap:6px}
.more{color:var(--blue);font-size:15px;white-space:nowrap}.more::after{content:"";display:inline-block;width:7px;height:7px;margin-left:8px;border:solid;border-width:0 2px 2px 0;transform:translateY(-3px) rotate(45deg);transition:transform .25s var(--ease)}
details[open]>summary{border-radius:14px 14px 0 0}details[open] .more::after{transform:translateY(1px) rotate(225deg)}
.opts{margin:0 0 0 20px;padding:16px 20px 20px 0;border-top:.5px solid var(--line)}.opts>.actions{padding-top:16px;border-top:.5px solid var(--line)}
.state{display:flex;align-items:center;gap:10px;color:var(--text);font-weight:600}.state::before{background:var(--faint)}.state.on::before{background:var(--green);box-shadow:0 0 0 4px rgba(52,199,89,.22)}
.state+.soft{font-size:15px}.tally{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 14px;padding:0;list-style:none}
.tally li{margin:0;padding:12px;border-radius:12px;background:var(--well);color:var(--text);font-weight:600;font-size:15px;font-variant-numeric:tabular-nums}
ol{margin:16px 0;padding:0;list-style:none;counter-reset:step}ol li{position:relative;min-height:28px;margin:0;padding:11px 0 11px 44px;counter-increment:step}
ol li+li::after{content:"";position:absolute;top:0;left:44px;right:0;border-top:.5px solid var(--line)}ol li::before{content:counter(step);position:absolute;left:0;top:10px;display:grid;place-items:center;width:28px;height:28px;border-radius:50%;background:var(--tint);color:var(--blue);font:600 14px var(--sans)}
ol+form{padding-top:20px;border-top:.5px solid var(--line)}code,pre,.code{font-family:var(--mono)}code{padding:.1em .4em;border-radius:6px;background:#fff;color:var(--text);font-size:.86em;overflow-wrap:anywhere}
pre{margin:12px 0;padding:14px 16px;max-height:420px;overflow:auto;border-radius:12px;background:var(--well);color:var(--text);font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}
ol li>.button:first-child{margin:-7px 0}.code{width:fit-content;max-width:100%;margin:10px 0 0;padding:12px 18px;border-radius:12px;background:var(--panel);color:var(--text);font-weight:600;font-size:28px;line-height:1.2;letter-spacing:.12em;user-select:all}
.out header{position:static;box-shadow:none;background:none;-webkit-backdrop-filter:none;backdrop-filter:none}.out .bar{justify-content:center;padding-top:64px}.out .brand{flex-direction:column;gap:14px;font-size:28px}
.out section{background:var(--panel)}.out main{max-width:420px;padding-top:12px}.out h1{font-size:28px;text-align:center}.out form button{width:100%}.out .actions{justify-content:center}
h2.big{margin:40px 0 12px;padding:0;color:var(--text);font:700 26px/1.15 var(--display);letter-spacing:-.03em;text-transform:none}.center{text-align:center}
.k0{--k:#0071e3}.k1{--k:#34c759}.k2{--k:#ff9500}.k3{--k:#af52de}.k4{--k:#ff2d55}.k5{--k:#30b0c7}.k6{--k:#5856d6}.k7{--k:#a2845e}
.calbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin:0 0 6px}.calbar h1{margin:0}.tools{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.seg{display:inline-flex;gap:2px;padding:3px;border-radius:10px;background:var(--panel);font-size:13px}.seg a{padding:6px 14px;border-radius:7px;color:var(--text);font-weight:500;text-decoration:none}.seg a[aria-current]{background:var(--thumb);font-weight:600;box-shadow:var(--thumb-shadow)}
.step{display:inline-flex;gap:6px}.step a{display:grid;place-items:center;min-width:36px;height:36px;padding:0 12px;border-radius:999px;background:var(--panel);color:var(--text);font-weight:600;text-decoration:none}.step a:hover{background:var(--hover)}
.range{margin:14px 0 12px;color:var(--text);font:600 20px/1.2 var(--display);letter-spacing:-.02em}
.legend{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}.legend a{display:inline-flex;align-items:center;gap:8px;min-height:34px;padding:6px 14px;border-radius:999px;background:var(--panel);color:var(--text);font-size:14px;font-weight:500;text-decoration:none}
.legend a[aria-current]{background:var(--text);color:#fff}.dot{flex:none;width:10px;height:10px;border-radius:50%;background:var(--k)}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--line);border-radius:16px;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
.grid>div{min-width:0;padding:8px;background:#fff}.grid>.wd{padding:8px;background:var(--panel);color:var(--dim);font-size:12px;font-weight:600;text-align:center;text-transform:uppercase}
.grid.week>div{min-height:300px}.grid.month>div:not(.wd){min-height:116px}.grid>.other{background:#fafafa}.grid>.other .dn{color:var(--faint)}
.dh{display:flex;align-items:center;gap:6px;margin:0 0 8px}.dn{display:inline-grid;place-items:center;min-width:28px;height:28px;padding:0 4px;border-radius:999px;color:var(--text);font-size:15px;font-weight:600}.wn{color:var(--dim);font-size:13px;font-weight:500}
.today .dn{background:var(--blue);color:#fff}
.ev{margin:0 0 4px;padding:4px 6px 4px 8px;border-left:3px solid var(--k);border-radius:6px;background:color-mix(in srgb,var(--k) 13%,#fff);color:var(--text);font-size:12px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev .t{margin-right:5px;color:var(--dim);font-variant-numeric:tabular-nums}.grid.week .ev{white-space:normal}.grid.week .ev .t{display:block;margin:0}
.ev.copy{border-left-style:dashed;background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--k) 12%,#fff) 0 6px,#fff 6px 12px)}
.plus{display:block;font-size:12px;font-weight:500}.calnote{margin:14px 0 0;color:var(--dim);font-size:13px}
@media (max-width:760px){.grid.week{grid-template-columns:1fr}.grid.week>div{min-height:0}.grid.month>div:not(.wd){min-height:64px;padding:4px}.grid.month .ev{padding:2px 4px;font-size:10px}.grid.month .ev .t{display:none}.grid>.wd{padding:6px 0;font-size:10px}.dn{min-width:24px;height:24px;font-size:13px}}
.adv{margin:-4px 0 12px;padding:0 20px;font-size:14px}section.warn{background:var(--bad-bg);color:var(--text)}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.quick{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 6px}.quick input[type=text]{flex:1 1 320px;width:auto}.quick select{flex:0 1 180px;width:auto}
.qpre{margin:10px 0 4px;box-shadow:0 0 0 1px var(--line)}.qline{font-size:17px}.warn{padding:10px 14px;border-radius:12px;background:var(--bad-bg);color:var(--bad)}
.evd{margin:0 0 4px}.evd>summary{display:block;margin:0;padding:4px 6px 4px 8px;border-radius:6px;list-style:none;cursor:pointer}.evd>summary::-webkit-details-marker{display:none}.evd>summary:hover{background:color-mix(in srgb,var(--k) 22%,#fff)}
.evd[open]>summary{border-radius:6px 6px 0 0}.evm{padding:8px;border:1px solid var(--line);border-top:0;border-radius:0 0 8px 8px;background:#fff;font-size:12px}.evm p{margin:0 0 6px}.evm button{min-height:30px;padding:4px 12px;font-size:13px}
.ev.gone{opacity:.5;text-decoration:line-through}
@media (min-width:900px){.cols{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:0 32px;align-items:start}.cols>div>h2:first-child{margin:0 0 20px;padding:0;color:var(--text);font:700 34px/1.1 var(--display);letter-spacing:-.03em;text-transform:none}}
@media (max-width:560px){header{position:static}.bar{padding:10px 16px}nav{flex:1 1 100%}nav a{flex:1;padding:6px 4px;text-align:center}main{padding:24px 16px 72px}h1{font-size:30px}
section{padding:16px}.list,.cal{padding:0}.row,summary{padding:12px 16px}.row+.row::before{left:68px}.opts{margin-left:16px;padding:16px 16px 16px 0}.chips{order:1;flex-basis:100%;padding-left:48px}.row.long>div{flex-basis:100%}.tally li{padding:10px;font-size:14px}.out .bar{padding-top:40px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""


# ---------- the rest of the stylesheet: sync rules, activity, the time grid. Positions are classes, since the content
# policy allows no inline styles: .s0-.s95 put an event at a quarter hour, .l1-.l96 give its length, .cI-N its column.

SLOT = 12               # px per quarter hour in the day and week views
CSS += """
.k8{--k:#e3342f}.k9{--k:#ffcc00}.k10{--k:#248a3d}.k11{--k:#8e8e93}
.pad{padding:0 20px 12px}.pairs{padding:0}.pair{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:12px 20px}.pair+.pair{border-top:.5px solid var(--line)}
.pair .who{flex:1 1 200px}.pair form{display:inline-flex}.pair .button,.pair button{min-height:32px;padding:5px 14px;font-size:14px}
.st{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;color:var(--dim)}.st::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--faint)}
.st.synced::before{background:var(--green)}.st.syncing::before{background:var(--blue)}.st.error{color:var(--bad)}.st.error::before{background:var(--bad)}
.arrow{color:var(--faint);padding:0 4px}.two{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px}@media (max-width:560px){.two{grid-template-columns:1fr}}
fieldset{margin:16px 0 0;padding:0;border:0}legend{padding:0;margin:0 0 6px;color:var(--text);font-size:15px;font-weight:600}
.inline-fields{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.inline-fields input[type=text],.inline-fields select,.inline-fields input[type=time],.inline-fields input[type=date],.inline-fields input[type=number]{width:auto;flex:1 1 140px}
input[type=time],input[type=date],input[type=number],input[type=search],textarea{display:block;width:100%;min-height:44px;padding:10px 14px;border:1px solid rgba(0,0,0,.12);border-radius:10px;background:var(--field);color:var(--text);font:inherit}
textarea{min-height:96px;resize:vertical}.days{display:flex;flex-wrap:wrap;gap:6px}.days label.check{margin:0;padding:6px 12px;border-radius:999px;background:#fff;font-size:15px}
.sample{margin:8px 0 0;padding:10px 14px;border-radius:10px;background:#fff;box-shadow:inset 0 0 0 .5px var(--line);font-size:15px}.sample b{font-weight:600}
.swatches{display:flex;flex-wrap:wrap;gap:6px}.swatches label.check{margin:0;align-items:center;gap:6px;padding:4px 10px 4px 6px;border-radius:999px;background:#fff;font-size:14px}
.why{padding:0}.why details{border-top:.5px solid var(--line)}.why details:first-child{border-top:0}.why summary{padding:10px 20px;border-radius:0;font-size:15px}.why .soft{font-size:13px}
.why ul{margin:0;padding:0 20px 12px 20px;list-style:none;font-size:14px}.why li{padding:3px 0}.why li b{font-weight:600}.yes{color:#248a3d}.no{color:var(--dim)}
.runs{padding:0}.run>summary{padding:12px 20px}.run+.run{border-top:.5px solid var(--line)}.run pre{margin:0 20px 16px}
.calbar .tools form{display:inline-flex}.tools input[type=search]{width:220px;min-height:36px;padding:6px 14px;border-radius:999px}
.toggles{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}.toggles button{min-height:30px;padding:4px 12px;font-size:13px;background:var(--panel);color:var(--text);font-weight:500}.toggles button[aria-pressed=true]{background:var(--text);color:#fff}
.tg{display:grid;border-radius:16px;overflow:hidden;box-shadow:0 0 0 1px var(--line);background:#fff}
.tg-head,.tg-all,.tg-body{display:grid;grid-template-columns:var(--gut,56px) repeat(var(--n,7),minmax(0,1fr))}
.tg.d1{--n:1}.tg.d5{--n:5}.tg.d7{--n:7}.tg.tz2{--gut:128px}.tg-hours span{white-space:nowrap}.tg-head .zones{font-size:10px;font-weight:500;color:var(--faint);text-align:right;padding:8px 6px}
.tg-head>div{padding:8px;background:var(--panel);text-align:center;font-size:13px;color:var(--dim);font-weight:600}.tg-head .today .dn{background:var(--blue);color:#fff}
.tg-all>div{min-height:28px;padding:3px;border-top:1px solid var(--line);border-left:1px solid var(--line)}.tg-all>div:first-child{border-left:0;font-size:11px;color:var(--faint);text-align:right;padding-right:8px}
.tg-scroll{max-height:70vh;overflow-y:auto;scrollbar-gutter:stable}.tg-head,.tg-all{overflow-y:hidden;scrollbar-gutter:stable}.tg-body{position:relative}
.tg-hours{display:grid;grid-auto-rows:48px;font-size:11px;color:var(--faint);text-align:right}.tg-hours span{padding:0 8px;transform:translateY(-7px)}
.tg-hours.two{grid-template-columns:1fr 1fr}
.tg-day{position:relative;height:1152px;border-left:1px solid var(--line);background:repeating-linear-gradient(#fff 0 47px,var(--line) 47px 48px)}
.tg-day.today{background:repeating-linear-gradient(#f7fbff 0 47px,var(--line) 47px 48px)}.tg-day.wk{background:repeating-linear-gradient(#fbfbfd 0 47px,var(--line) 47px 48px)}
.tg-day .ev,.tg-day .evl{position:absolute;left:2px;right:2px;margin:0;white-space:normal;overflow:hidden;z-index:1}
.ev a,.evl{color:inherit;text-decoration:none}.evl{display:block}.evl:hover{text-decoration:none;filter:brightness(.97)}
.ev.pend{outline:2px dashed var(--blue);outline-offset:-2px}.now{position:absolute;left:0;right:0;height:2px;background:var(--bad);z-index:2}
.ev .rs{position:absolute;left:0;right:0;bottom:0;height:6px;cursor:ns-resize}.ev[data-move]{cursor:grab}.dragging{opacity:.85;box-shadow:0 8px 24px rgba(0,0,0,.18);z-index:5}
.wkn{color:var(--faint);font-size:11px;font-weight:600}.grid.month.nowe{grid-template-columns:repeat(5,minmax(0,1fr))}
.evhead{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}.evhead h1{margin:0}.facts{margin:10px 0 0;padding:0;list-style:none}.facts li{padding:4px 0}
.rsvp{display:inline-flex;gap:6px}.rsvp button{min-height:34px;padding:5px 14px;font-size:15px}.rsvp button[aria-pressed=true]{background:var(--text);color:#fff}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:20;display:flex;align-items:center;gap:12px;padding:10px 12px 10px 18px;border-radius:999px;background:#1d1d1f;color:#fff;font-size:15px;font-weight:600;box-shadow:0 12px 32px rgba(0,0,0,.25)}
.toast button{min-height:32px;padding:4px 14px;background:rgba(255,255,255,.16);color:#fff;font-size:15px}.toast .soft{color:rgba(255,255,255,.6);font-weight:500}
.results{padding:0}.results .row{padding:10px 20px}.results .when{flex:0 0 150px;color:var(--dim);font-size:14px;font-variant-numeric:tabular-nums}
.keys{font-size:13px;color:var(--dim)}kbd{display:inline-block;min-width:20px;padding:1px 6px;border-radius:6px;background:#fff;box-shadow:inset 0 0 0 .5px var(--line),0 1px 0 var(--line);font:600 12px var(--sans);text-align:center}
@media (max-width:560px){.tg{--gut:38px}.tg.tz2{--gut:84px}.tg-hours span{padding:0 3px;font-size:9px}.tg-day .ev{overflow-wrap:normal;word-break:keep-all;hyphens:none}.tg-head .zones{font-size:0}.tg.d7 .ev .t,.tg.d5 .ev .t{display:none}.tg-day .ev{padding:2px 3px;font-size:10px;border-left-width:2px}}
@media (max-width:760px){.tg.d7{--n:7}.tg-head>div{padding:6px 0;font-size:11px}.tg-day .ev{font-size:10px;padding:2px 3px}.tools input[type=search]{width:100%}}
@media print{header,.tools,.toggles,.quick,.legend,.calnote,.toast,.flash,.noprint{display:none!important}main,main.wide{max-width:none;padding:0}.tg-scroll{max-height:none;overflow:visible}body{font-size:12px}.grid,.tg{box-shadow:none;border:1px solid #ccc}}
"""
CSS += "".join(f".s{i}{{top:{i * SLOT}px}}" for i in range(96))
CSS += "".join(f".l{i}{{height:{i * SLOT - 2}px}}" for i in range(1, 97))
CSS += "".join(f".tg-day .c{i}-{n}{{left:calc({i}/{n}*100% + 2px);right:auto;width:calc(100%/{n} - 4px)}}" for n in range(2, 5) for i in range(n))

# One same-origin script, only for what plain links and forms can't do: keyboard keys, dragging events, the
# undo countdown and the live tag preview. Every page works without it.
JS = r"""
(function(){
var $=function(s,r){return (r||document).querySelector(s)},$$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var body=document.body,cal=$('[data-cal-page]');
function go(u){if(u)location.href=u}
document.addEventListener('keydown',function(e){
  var t=e.target,typing=/INPUT|TEXTAREA|SELECT/.test(t.tagName)||t.isContentEditable;
  if(e.key==='Escape'){var x=$('[data-close]');if(x&&!typing)go(x.getAttribute('href'));if(typing)t.blur();return}
  if(typing||e.metaKey||e.ctrlKey||e.altKey||!cal)return;
  var k=e.key.toLowerCase(),m={d:'data-day',w:'data-week',m:'data-month',t:'data-today',n:'data-new',j:'data-next',k:'data-prev',arrowright:'data-next',arrowleft:'data-prev'};
  if(k==='/'){var s=$('#search');if(s){e.preventDefault();s.focus()}return}
  if(k==='?'){var h=$('#keys');if(h)h.hidden=!h.hidden;return}
  if(k==='p'){window.print();return}
  if(m[k]){var a=$('['+m[k]+']');if(a){e.preventDefault();go(a.getAttribute('href'))}}
});
$$('[data-print]').forEach(function(b){b.hidden=false;b.addEventListener('click',function(e){e.preventDefault();window.print()})});
var sc=$('.tg-scroll');if(sc&&!location.hash)sc.scrollTop=7*48;
$$('[data-until]').forEach(function(el){
  var until=+el.getAttribute('data-until'),n=$('.left',el);
  (function tick(){var s=Math.max(0,Math.ceil((until-Date.now())/1000));if(n)n.textContent=s+' s';if(s<=0){el.remove();return}setTimeout(tick,250)})();
});
var tag=$('#tag'),tt=$('#title_text'),showTitle=$('#copy_title');
function sample(){var p=$('#tagprev');if(!p)return;var t=(showTitle&&!showTitle.checked)?((tt&&tt.value)||'Busy'):p.getAttribute('data-title');p.textContent=((tag&&tag.value)?tag.value+' ':'')+t}
[tag,tt,showTitle].forEach(function(i){if(i){i.addEventListener('input',sample);i.addEventListener('change',sample)}});
$$('[data-oauth-wait]').forEach(function(a){a.addEventListener('click',function(){var w=a.getAttribute('data-oauth-wait');setTimeout(function(){go(w)},400)})});
var off=$('#enabled');function keep(){var k=$('#keepbox');if(k&&off)k.hidden=off.checked}if(off){off.addEventListener('change',keep);keep()}
// drag to move or resize, in the day and week views: quarter-hour steps, across days
var grid=$('.tg-body');if(!grid)return;
var csrf=grid.getAttribute('data-csrf'),slot=12;
function cols(){return $$('.tg-day',grid)}
function post(url,data){var f=new URLSearchParams(data);f.append('csrf',csrf);return fetch(url,{method:'POST',body:f,credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'}})}
grid.addEventListener('dblclick',function(e){var d=e.target.closest('.tg-day');if(!d||e.target.closest('.ev'))return;
  var y=e.clientY-d.getBoundingClientRect().top,q=Math.floor(y/slot),h=Math.floor(q/4),mi=(q%4)*15;
  go(d.getAttribute('data-new')+'&t='+(h<10?'0':'')+h+':'+(mi<10?'0':'')+mi)});
grid.addEventListener('pointerdown',function(e){
  var ev=e.target.closest('.ev[data-move]');if(!ev||e.button!==0)return;
  var resize=e.target.classList.contains('rs'),startY=e.clientY,startX=e.clientX,top=ev.offsetTop,h=ev.offsetHeight,col=ev.parentNode,moved=false,target=col;
  var list=cols(),ci=list.indexOf(col);
  function mv(m){var dy=Math.round((m.clientY-startY)/slot)*slot;if(Math.abs(m.clientY-startY)>4||Math.abs(m.clientX-startX)>8)moved=true;if(!moved)return;
    ev.classList.add('dragging');
    if(resize){ev.style.height=Math.max(slot-2,h+dy)+'px'}else{ev.style.top=Math.max(0,Math.min(1152-h,top+dy))+'px';
      var w=col.getBoundingClientRect().width,di=Math.round((m.clientX-startX)/w),ni=Math.max(0,Math.min(list.length-1,ci+di));
      if(list[ni]!==target){target=list[ni];target.appendChild(ev)}}}
  function up(){document.removeEventListener('pointermove',mv);document.removeEventListener('pointerup',up);
    if(!moved)return;
    var q0=Math.round(ev.offsetTop/slot),len=Math.max(1,Math.round((ev.offsetHeight+2)/slot)),day=target.getAttribute('data-date');
    var t=function(q){var hh=Math.floor(q/4),mm=(q%4)*15;return (hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm};
    var data={cal:ev.getAttribute('data-cal'),uid:ev.getAttribute('data-uid'),href:ev.getAttribute('data-href')||'',date:day,start:t(q0),end:t(Math.min(96,q0+len)),back:location.pathname+location.search};
    if(ev.getAttribute('data-move')==='ask'){go('/event?'+new URLSearchParams(data).toString()+'&edit=1');return}
    post('/event/move',data).then(function(r){return r.json()}).then(function(j){if(j.go)location.href=j.go;else location.reload()},function(){location.reload()})}
  document.addEventListener('pointermove',mv);document.addEventListener('pointerup',up);e.preventDefault()});
grid.addEventListener('click',function(e){var ev=e.target.closest('.ev.dragging');if(ev){e.preventDefault();e.stopPropagation()}},true);
})();
"""


NAV = (("/", "Sync"), ("/calendar", "Calendar"), ("/settings", "Settings"))


def page(title, body, signed_in=False, flash=None, refresh=None, at="", wide=False):
    """at is the header link this page belongs under; wide gives the two-column sync page room."""
    links = "".join('<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if href == at else "", name) for href, name in NAV)
    nav = f'<nav aria-label="Main">{links}</nav>' if signed_in else ""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    note = f'<p class="flash" role="status">{esc(flash)}</p>' if flash else ""
    size = 32 if signed_in else 64
    script = '<script src="/app.js" defer></script>' if signed_in else ""
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="theme-color" content="#ffffff">{meta}'
            f'<title>{esc(title)} · CalSync</title><link rel="stylesheet" href="/app.css"><link rel="icon" href="{MARK}">'
            f'<body class="{"in" if signed_in else "out"}"><header><div class="bar"><a class="brand" href="/"><img src="{MARK}" alt="" width="{size}" height="{size}">CalSync</a>{nav}</div></header>'
            f'<main{" class=wide" if wide else ""}>{note}{body}</main>{script}</html>')


def tile(name):
    """The square with a calendar's or an account's first letter, on the tile of calsync's icon."""
    return f'<span class="tile" aria-hidden="true">{esc(str(name)[:1].upper())}</span>'


GOOGLE_STEPS = """
<ol>
<li>Open <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">console.cloud.google.com</a> and create a project called calsync.</li>
<li>Go to APIs &amp; Services, Library, find <b>Google Calendar API</b> and click Enable.</li>
<li>Go to Google Auth Platform and click Get started. Name it CalSync, pick your email, choose <b>External</b>.</li>
<li>Go to Audience and click <b>Publish app</b>. Unverified is fine. An app left in Testing signs you out every 7 days.</li>
<li>Go to Clients, Create client, type <b>Desktop app</b>. Copy the client ID and secret here.</li>
</ol>"""

MICROSOFT_STEPS = """
<ol>
<li>Open <a href="https://entra.microsoft.com/" target="_blank" rel="noreferrer">entra.microsoft.com</a>, then App registrations, New registration.</li>
<li>Name it CalSync and choose <b>Accounts in any organizational directory and personal Microsoft accounts</b>. Leave the redirect empty.</li>
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
        return page("Sync", f'<h1>Your calendars</h1><section class="empty"><img src="{MARK}" alt="" width="44" height="44"><p>Nothing is connected yet. Start by adding your first calendar below.</p></section>'
                    f'<h2 id="add" class="big">Add a calendar</h2>{connect_body(csrf)}', True, flash, at="/")
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
<label for="col-{esc(c["name"])}">Colour on the Calendar tab</label><select id="col-{esc(c["name"])}" name="colour"><option value="">Automatic</option>{"".join(f'<option value="{k}"{" selected" if c.get("colour") == k else ""}>{k.title()}</option>' for k in engine.PALETTE)}</select>
<p class="soft">What is copied where, and filters, are set per direction under What goes where.</p>
{skip}<div class="actions"><button>Save</button></div></form>
<div class="actions"><form class="inline" method="post" action="/calendar/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="plain">Stop syncing this calendar</button></form>
<form class="inline" method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="danger">Delete the copies CalSync made here</button></form></div></div></details></section>''')
    if len(cals) < 2:
        out.append('<section class="note">Add at least two calendars and CalSync can start.</section>')
    out.append(f'<div class="actions"><a class="button plain" href="#add">Add a calendar</a><a class="button plain" href="/calendar">See them on a calendar</a></div>'
               f'<h2 id="add" class="big">Add a calendar</h2>{connect_body(csrf)}</div><div><h2>Sync</h2>')
    fast = " Google and Microsoft calendars are checked every minute." if cfg.get("enabled") and cfg.get("fast_check", True) else ""
    state = ("Running in the background every " + str(cfg.get("interval_minutes", 5)) + " minutes." + fast) if cfg.get("enabled") else "Not running yet. Preview first, then start it."
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
        out.append(why_section(preview.get("decisions"), cfg))
    out.append(pairs_section(csrf, cfg))
    return page("Sync", "".join(out) + "</div></div>", True, flash, at="/", wide=True)


def connect_body(csrf):
    accounts = engine._accounts()
    rows = []
    for label, a in accounts.items():
        kind = a.get("service") or {"google": "Google", "outlook": "Microsoft", "caldav": "CalDAV", "ics": ".ics link"}.get(a["provider"], "")
        rows.append(f'''<div class="row">{tile(label)}<div><b>{esc(label)}</b> <span class="soft">{kind}</span></div><div class="actions flat"><a class="button" href="/account?label={urllib.parse.quote(label)}">Choose calendars</a>
<form class="inline" method="post" action="/account/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="label" value="{esc(label)}"><button class="plain">Sign out</button></form></div></div>''')
    existing = ('<h2>Signed in accounts</h2><section class="list">' + "".join(rows) + "</section>") if rows else ""
    caldav = "".join(f'''<div class="row"><div><b>{"Another CalDAV server" if k == "other" else v[0]}</b></div><a class="button" href="/connect/caldav?p={k}">Connect</a></div>'''
                     for k, v in engine.CALDAV_SERVERS.items())
    one = {**oneclick("google"), **oneclick("microsoft")}
    sign = lambda k, name: (f'<a class="button" href="{esc(one[k])}" target="_blank" rel="noopener" data-oauth-wait="/connect/wait?flow={esc(one[k + "_state"])}">Sign in with {name}</a>'
                            if one.get(k) else f'<a class="button" href="/connect/{k}">Connect</a>')
    unverified = (' <span class="soft">Google may first say it hasn\'t verified CalSync while its review is pending. Click Advanced, then Go to CalSync.</span>'
                  if engine.GOOGLE_UNVERIFIED else "")
    advanced = ('<p class="soft adv"><a href="/connect/advanced">Advanced: use your own OAuth app</a></p>' if one.get("google") or one.get("microsoft")
                else '<p class="soft adv">One-click sign-in works when you open CalSync on the computer it runs on. From here, use your own sign-in app. <a href="/connect/advanced">More</a></p>' if one.get("remote") else "")
    return f'''{existing}<h2>Connect an account</h2>
<section class="list"><div class="row"><div><b>Google Calendar</b>{unverified if one.get("google") else ""}</div>{sign("google", "Google")}</div>
<div class="row"><div><b>Outlook or Microsoft 365</b> <span class="soft">Work and school accounts too, unless your organisation blocks outside apps.</span></div>{sign("microsoft", "Microsoft")}</div>
{caldav}</section>{advanced}
<section class="list"><div class="row long"><div><b>Published .ics link</b> <span class="soft">Read-only: its events are copied out, nothing is written back. For Proton Calendar, an Exchange server, or an organization that blocks outside apps.</span></div><a class="button" href="/connect/ics">Add link</a></div>
<div class="row long"><div><b>Calendly, Cal.com or another booking app</b> <span class="soft">Nothing to add here. Connect the booking app to any calendar above. CalSync keeps that calendar showing your busy times from all the others.</span></div><a class="button plain" href="https://docs.getcalsync.app/booking-apps/">How it works</a></div></section>'''


def oneclick(provider):
    """A ready sign-in link for CalSync's own app, made for this page view: {provider: url, provider_state: state}.
    Only when the page was opened on this computer, since the sign-in comes back to 127.0.0.1 or localhost."""
    if not engine.builtin_app(provider):
        return {}
    name = re.sub(r":\d+$", "", (getattr(here, "host", "") or "").strip().lower()).strip("[]")
    if PUBLIC_URL or not (name in ("localhost", "127.0.0.1", "::1") or name.endswith(".localhost")):
        return {"remote": True}
    port = re.search(r":(\d+)$", getattr(here, "host", "") or "")
    redirect = engine.loopback_redirect(provider, port.group(1) if port else 80)
    flow = engine.oauth_start(provider, redirect)
    with state_lock:
        if len(pending_oauth) >= MAX_OAUTH_FLOWS:       # each page view makes one; the oldest unused go first
            for key in sorted(pending_oauth, key=lambda k: pending_oauth[k]["expires"])[:len(pending_oauth) - MAX_OAUTH_FLOWS + 1]:
                del pending_oauth[key]
        pending_oauth[flow["state"]] = {"provider": provider, "verifier": flow["verifier"], "redirect": redirect,
                                        "expires": time.time() + 900, "status": "waiting"}
    return {provider: flow["url"], provider + "_state": flow["state"]}


def advanced_page(csrf, flash=None):
    return page("Your own sign-in app", '''<h1>Advanced: use your own OAuth app</h1><section><p>CalSync's own Google and Microsoft apps are the quick way. If you run CalSync on a server,
or would rather your sign-ins go through an app you own, make your own. It takes about 10 minutes each.</p></section>
<section class="list"><div class="row"><div><b>Your own Google app</b> <span class="soft">A Desktop app client from Google Cloud, with its ID and secret.</span></div><a class="button" href="/connect/google">Set up</a></div>
<div class="row"><div><b>Your own Microsoft app</b> <span class="soft">An Entra app registration, signed in with a code.</span></div><a class="button" href="/connect/microsoft">Set up</a></div></section>
<div class="actions"><a class="button plain" href="/#add">Back</a></div>''', True, flash, at="/")


def blocked_section():
    return ('<section class="warn"><p><b>Your organisation needs an administrator to approve CalSync.</b> Many schools and workplaces do this for '
            'outside apps. You can ask your IT team, or sync this calendar one way: in Outlook on the web, publish the calendar and '
            'add its .ics link here.</p><div class="actions"><a class="button" href="/connect/ics">Add an .ics link</a>'
            '<a class="button plain" href="https://docs.getcalsync.app/ics/">How to publish it</a></div></section>')


def wait_page(flow_id):
    with state_lock:
        flow = pending_oauth.get(flow_id)
    if not flow:
        return None, page("Sign in", '<h1>This sign-in has expired</h1><section class="note">Start again from Add a calendar.</section><a class="button plain" href="/#add">Add a calendar</a>', True, at="/")
    name = "Google" if flow["provider"] == "google" else "Microsoft"
    google_hint = ('<p>If Google says <b>Google hasn\'t verified this app</b>, that\'s expected for now: Google is still reviewing CalSync. '
                   'Click <b>Advanced</b>, then <b>Go to CalSync</b>, then <b>Continue</b>.</p>')
    if flow["status"] == "done":
        return "/account?label=" + urllib.parse.quote(flow["label"]), None
    if flow["status"] == "blocked":
        return None, page("Sign in", f'<h1>{name} didn\'t let CalSync in</h1>{blocked_section()}', True, at="/")
    if flow["status"] == "failed":
        return None, page("Sign in", f'<h1>That didn\'t work</h1><section class="bad">{esc(flow.get("problem", ""))}</section><a class="button plain" href="/#add">Try again</a>', True, at="/")
    return None, page("Sign in", f'''<h1>Sign in to {name}</h1><section><p>Your browser opened {name}. Choose your account and allow CalSync.
This page moves on by itself when you're done.</p>{google_hint if flow["provider"] == "google" and engine.GOOGLE_UNVERIFIED else ""}<p class="soft">If no browser window opened, <a href="/#add">go back</a> and press Sign in with {name} again.</p></section>''', True, refresh=2, at="/")


def connect_page(csrf, flash=None):
    """Adding a calendar is part of the sync page now."""
    return dashboard(csrf, flash)


def google_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "google-client.json"), None)
    if client:
        return page("Connect Google", f'''<h1>Connect Google</h1><section><form method="post" action="/connect/google/start"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="work">
<div class="actions"><button>Continue</button></div></form></section>
<section><p>Your Google sign-in app is saved. Replace it only if you made a new one.</p>{client_form(csrf, True)}</section>''', True, flash, at="/")
    return page("Connect Google", f'<h1>Your own Google app</h1><section><p>Make a sign-in app of your own in Google Cloud. It\'s free and takes about 10 minutes. You only do it once, for all your Google accounts.</p>{GOOGLE_STEPS}{client_form(csrf)}</section>', True, flash, at="/")


def client_form(csrf, replacing=False):
    """Replacing a saved app is the side road, Continue is the way on, so its Save is the quieter button."""
    return f'''<form method="post" action="/connect/google/client" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Client ID</label><input type="text" id="cid" name="client_id" required><label for="sec">Client secret</label><input type="password" id="sec" name="client_secret" required>
<div class="actions"><button{" class=plain" if replacing else ""}>Save</button></div></form>'''


def microsoft_page(csrf, flash=None):
    client = engine.load_json(os.path.join(engine.SECRETS, "microsoft-client.json"), None)
    steps = "" if client else f"<p>Microsoft needs a sign-in app of your own. It's free and you only make it once.</p>{MICROSOFT_STEPS}"
    cid = esc(client["client_id"]) if client else ""
    return page("Connect Microsoft", f'''<h1>Your own Microsoft app</h1><section>{steps}<form method="post" action="/connect/microsoft/start" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="cid">Application (client) ID</label><input type="text" id="cid" name="client_id" required value="{cid}">
<label for="label">A short name for this account</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="school">
<div class="actions"><button>Continue</button></div></form></section>''', True, flash, at="/")


def ics_page(csrf, flash=None):
    return page("Add an .ics link", f'''<h1>Add a published .ics link</h1><section><p>CalSync saves this link and never shows it again, since anyone who has it can read that calendar.</p>
<form method="post" action="/connect/ics/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="classes">
<label for="url">Calendar link</label><input type="password" id="url" name="url" required>
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash, at="/")


def caldav_page(kind, csrf, flash=None):
    name, server, note = engine.CALDAV_SERVERS[kind]
    address = "" if server else '<label for="server">Server address</label><input type="text" id="server" name="server" required placeholder="https://cloud.example.com">'
    return page(f"Connect {name}", f'''<h1>Connect {name}</h1><section><p>{note}</p><p>CalSync saves this password on this machine and never shows it again.</p>
<form method="post" action="/connect/caldav/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="kind" value="{kind}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="personal">
{address}<label for="username">Username</label><input type="text" id="username" name="username" required>
<label for="password">Password</label><input type="password" id="password" name="password" required autocomplete="new-password">
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash, at="/")


def account_page(label, csrf, flash=None):
    try:
        found = engine.account_calendars(label)
    except Exception as e:
        return page("Choose calendars", f'<h1>{esc(label)}</h1><section class="bad">Couldn\'t read this account\'s calendars: {esc(short_error(e))}</section>', True, flash, at="/")
    used = {(c.get("account"), c.get("calendar_id")) for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
    rows = []
    for i, c in enumerate(found):
        # a Google calendar is often named after its address, and a name goes to anything that shows your calendars
        part = "main" if "@" in c["name"] else c["name"]
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{label.split('@')[0]}-{part}" if len(found) > 1 else label.split("@")[0]).strip("-_")[:40] or f"calendar-{i + 1}"
        if (label, c["id"]) in used:
            rows.append(f'<section class="list"><div class="row">{tile(c["name"])}<div><b>{esc(c["name"])}</b></div><span class="chip">already syncing</span></div></section>')
            continue
        ro = "" if c["writable"] else '<p class="soft">CalSync can read this one but not write to it.</p>'
        rows.append(f'''<section><p class="head">{tile(c["name"])}<b>{esc(c["name"])}</b></p>{ro}<form method="post" action="/calendar/add"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="account" value="{esc(label)}"><input type="hidden" name="calendar_id" value="{esc(c["id"])}">
<label for="n{i}">Name in CalSync</label><input type="text" id="n{i}" name="name" value="{esc(slug)}" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*">
{'<label class="check"><input type="checkbox" name="busy_only"> Copies here only say Busy</label>' if c["writable"] else ""}
<div class="actions"><button>Sync this calendar</button></div></form></section>''')
    listed = "".join(rows) or '<section class="note">This account has no calendars.</section>'
    return page("Choose calendars", f'<h1>{esc(label)}: choose calendars</h1>{listed}<div class="actions"><a class="button plain" href="/">Back to sync</a></div>', True, flash, at="/")


def settings_page(csrf, flash=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    current = cfg.get("interval_minutes", 5)
    opts = "".join(f'<option value="{m}"{" selected" if m == current else ""}>Every {m} minutes</option>' for m in INTERVALS)
    made = ((web_config() or {}).get("recovery") or {}).get("made")
    writable = engine.writable_calendars(cfg)
    try:
        main = engine.main_calendar(cfg)["name"] if writable else ""
    except SystemExit:
        main = ""
    add_to = ("<label for=\"addto\">Quick add puts events on</label><select id=\"addto\" name=\"add_to\">" + "".join(
        f'<option value="{esc(c["name"])}"{" selected" if c["name"] == main else ""}>{esc(c.get("label") or c["name"])}</option>' for c in writable)
        + "</select><p class=\"soft\">Pick the calendar your booking page reads, so a new event blocks the time there at once.</p>") if writable else ""
    window = cfg.get("window") or {}
    recovery_state = f"You have a recovery code, made {esc(made)}." if made else "<b>You don't have a recovery code yet.</b> Make one now, so a forgotten password isn't a problem."
    return page("Settings", f'''<h1>Settings</h1><section><form method="post" action="/settings/save"><input type="hidden" name="csrf" value="{csrf}">
<label for="iv">How often to sync</label><select id="iv" name="interval">{opts}</select>
{add_to}<label for="tz">Your time zone</label><input type="text" id="tz" name="timezone" value="{esc(cfg.get("timezone") or engine.tz_name())}" required>
<label for="tz2">A second time zone beside it on the Calendar tab</label><input type="text" id="tz2" name="tz2" value="{esc(calendar_prefs(cfg)["tz2"])}" placeholder="Europe/London">
<fieldset><legend>How far to sync</legend><div class="inline-fields"><input type="number" name="past_days" min="0" max="365" value="{esc(window.get("past_days", engine.PAST_DAYS))}" aria-label="Days back"><span>days back and</span>
<input type="number" name="future_days" min="1" max="730" value="{esc(window.get("future_days", engine.FUTURE_DAYS))}" aria-label="Days ahead"><span>days ahead</span></div>
<p class="soft">Events outside this window aren't copied, and their copies are left as they are.</p></fieldset>
<label class="check"><input type="checkbox" name="fast_check" value="1"{" checked" if cfg.get("fast_check", True) else ""}> Check Google and Microsoft calendars for changes every minute, and sync as soon as one changes</label>
<p class="soft">{esc(push_words())}</p>
<div class="actions"><button>Save</button></div></form></section>
<h2>Change password</h2><section><form method="post" action="/settings/password" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="old">Current password</label><input type="password" id="old" name="old" required><label for="new">New password</label><input type="password" id="new" name="new" required minlength="10">
<div class="actions"><button>Change password</button></div></form></section>
<h2>Recovery code</h2><section><p>{recovery_state}</p><p class="soft">If you forget your password, the recovery code lets you choose a new one, but only on this computer. A new code replaces the old one.</p>
<form method="post" action="/settings/recovery" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label for="rcp">Current password</label><input type="password" id="rcp" name="old" required>
<div class="actions"><button class="plain">Make a new recovery code</button></div></form></section>
<div class="actions"><form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button class="plain">Sign out</button></form>
<form class="inline" method="post" action="/logout/everywhere"><input type="hidden" name="csrf" value="{csrf}"><button class="plain">Sign out on every device</button></form></div>''', True, flash, at="/settings")


# ---------- the Calendar tab: what calsync sees, as a week or a month ----------

CAL_CACHE = {}          # (calendar, window) -> (fetched at, ("ok", events) or ("err", message))
CAL_CACHE_SECONDS = 120
COLOURS = 8             # .k0 to .k7 in the stylesheet; colours stay put because they follow the order calendars were added in


def calendar_events(c, tmin, tmax, fresh=False):
    key = (c["name"], c.get("account"), c.get("calendar_id"), tmin, tmax)
    with state_lock:
        hit = CAL_CACHE.get(key)
    if hit and not fresh and time.time() - hit[0] < CAL_CACHE_SECONDS:
        return hit[1]
    try:
        got = ("ok", [e for e in engine.list_for_people(engine.make_adapter(c), tmin, tmax) if not e.get("declined") and not engine._canceled(e)])
    except Exception as e:
        got = ("err", short_error(e))
    with state_lock:
        if len(CAL_CACHE) > 200:
            CAL_CACHE.clear()
        CAL_CACHE[key] = (time.time(), got)
    return got


def _local(ev, tz):
    """(first day, last day, time label) in the viewer's zone. All-day ends are exclusive in the event model."""
    D = engine.dt
    if ev["all_day"]:
        first, end = D.date.fromisoformat(ev["start"][:10]), D.date.fromisoformat(ev["end"][:10])
        return first, max(first, end - D.timedelta(days=1)), "All day"
    s = D.datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    e = D.datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    last = (e - D.timedelta(seconds=1)).date() if e > s else s.date()
    label = s.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return s.date(), max(s.date(), last), label


def quick_preview(csrf, plan, text, cal, back):
    """The one-line preview, with whatever needs a yes: an overlap, guests who would get an invite."""
    ev = plan["event"]
    lines = [f'<p class="qline"><b>{esc(ev["title"])}</b> · {esc(ev["when"])} · on {esc(plan["calendar"])}</p>']
    if ev.get("remind_minutes") is not None:
        m = ev["remind_minutes"]
        lines.append(f'<p class="soft">Reminder {esc(f"{m // 60} h" if m and m % 60 == 0 else f"{m} min")} before.</p>')
    if plan["overlaps"]:
        lines.append('<p class="warn"><b>This overlaps ' + "; ".join(f'{esc(o["title"])}, {esc(o["when"])} ({esc(o["calendar"])})' for o in plan["overlaps"]) + ".</b></p>")
    if plan.get("unread"):
        lines.append(f'<p class="soft">Couldn&#x27;t check {esc(", ".join(plan["unread"]))} for overlaps just now.</p>')
    invite = ""
    if ev.get("guests"):
        lines.append('<p>Invites will be emailed to: <b>' + esc(", ".join(ev["guests"])) + '</b></p>')
        invite = '<label class="check"><input type="checkbox" name="send_invites" value="1" required> Send the invites to these people</label>'
    go = "Book anyway" if plan["overlaps"] else "Add it"
    return (f'<section class="qpre">{"".join(lines)}<form method="post" action="/quick/add"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="text" value="{esc(text)}"><input type="hidden" name="calendar" value="{esc(cal)}"><input type="hidden" name="back" value="{esc(back)}">'
            f'<input type="hidden" name="book_anyway" value="{"1" if plan["overlaps"] else ""}">{invite}'
            f'<div class="actions"><button{" class=danger" if plan["overlaps"] else ""}>{go}</button><a class="button plain" href="{esc(back)}">Cancel</a></div></form></section>')


def short_error(e):
    """Errors from the providers can be long; tokens never appear in them, since http() only reports the status and body."""
    return str(e)[:300]



# ---------- sync rules: one direction at a time ----------

STATUS_WORDS = {"synced": "Synced", "syncing": "Syncing", "error": "Error", "off": "Off", "waiting": "Not started"}


def colour_classes(cfg):
    """Calendar name -> its colour class: the colour chosen for it, else one by the order it was added in."""
    names = list(engine.PALETTE)
    return {c["name"]: f"k{names.index(c['colour']) if c.get('colour') in names else i % len(names)}" for i, c in enumerate(cfg["calendars"])}


def hidden_csrf(csrf):
    return f'<input type="hidden" name="csrf" value="{csrf}">'


def rule_summary(rule):
    if not rule["enabled"]:
        return "Off" + (", copies kept" if rule["keep_copies"] else "")
    cp = rule["copy"]
    bits = ["Title" if cp["title"] else f'Says "{cp["title_text"]}"']
    bits += [w for k, w in (("description", "notes"), ("location", "place"), ("conference", "join link"), ("attendees", "guests")) if cp[k]]
    f = rule["filters"]
    left = [w for k, w in (("free", "free"), ("tentative", "maybe"), ("unanswered", "unanswered"), ("all_day", "all-day"), ("ooo", "out of office")) if f[k]]
    if f["keywords"] or f["colours"] or f["days"] or f["from"] or f["until"]:
        left.append("more")
    return ", ".join(bits) + (" · leaves out " + ", ".join(left) if left else "")


def pairs_section(csrf, cfg):
    """Every direction calsync copies in, with its rule in a few words, its status and Resync."""
    cals = cfg["calendars"]
    label = {c["name"]: c.get("label") or c["name"] for c in cals}
    with state_lock:
        running = run_state["running"]
    status = engine.pair_status(cfg, running=running)
    rows = []
    for d in cals:
        if d.get("read_only"):
            continue
        for s in cals:
            if s["name"] == d["name"]:
                continue
            key = f"{s['name']}>{d['name']}"
            st, words = status.get(key, ("waiting", ""))
            rule = engine.rule_for(s["name"], d["name"], cfg)
            q = urllib.parse.urlencode({"src": s["name"], "dst": d["name"]})
            resync = (f'<form method="post" action="/resync">{hidden_csrf(csrf)}<input type="hidden" name="pair" value="{esc(key)}"><button class="plain">Resync</button></form>'
                      if rule["enabled"] and cfg.get("enabled") else "")
            rows.append(f'<div class="pair"><span class="who"><strong>{esc(label[s["name"]])}<span class="arrow">→</span>{esc(label[d["name"]])}</strong>'
                        f'<span class="soft">{esc(rule_summary(rule))}</span></span><span class="st {st}" title="{esc(words)}">{STATUS_WORDS[st]}</span>'
                        f'<a class="button plain" href="/pair?{esc(q)}">Edit</a>{resync}</div>')
    if not rows:
        return ""
    return f'<h2>What goes where</h2><section class="pairs">{"".join(rows)}</section><p class="soft center"><a href="/activity">Activity</a></p>'


def pair_page(csrf, src, dst, flash=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    label = {c["name"]: c.get("label") or c["name"] for c in cfg["calendars"]}
    rule = engine.rule_for(src, dst, cfg)
    cp, f = rule["copy"], rule["filters"]
    on = lambda v: " checked" if v else ""
    box = lambda name, text, v, extra="": f'<label class="check"><input type="checkbox" name="{name}" value="1"{on(v)}{extra}> {text}</label>'
    sample_title = "Team meeting"
    shown = f"{rule['tag'] + ' ' if rule['tag'] else ''}{sample_title if cp['title'] else cp['title_text']}"
    colours = "".join(f'<option value="{k}"{" selected" if rule["colour"] == k else ""}>{k.title()}</option>' for k in engine.PALETTE)
    swatch = "".join(f'<label class="check k{i}"><input type="checkbox" name="f_colours" value="{k}"{on(k in f["colours"])}><span class="dot"></span>{k.title()}</label>'
                     for i, k in enumerate(engine.PALETTE))
    days = "".join(f'<label class="check"><input type="checkbox" name="f_days" value="{i}"{on(i in f["days"])}>{d}</label>' for i, d in enumerate(engine.DAYS))
    mode = lambda name, v: (f'<select name="{name}" aria-label="How to use this list"><option value="exclude"{" selected" if v == "exclude" else ""}>Leave these out</option>'
                            f'<option value="only"{" selected" if v == "only" else ""}>Copy only these</option></select>')
    runs = engine.activity(10, f"{src}>{dst}")
    recent = "".join(f'<li>{esc(local_stamp(r["at"]))}: {esc(", ".join(r.get("lines", [])[:3]) or "no changes")}</li>' for r in runs if r.get("lines")) or "<li>Nothing copied this way yet.</li>"
    other = urllib.parse.urlencode({"src": dst, "dst": src})
    body = f'''<h1>{esc(label[src])} <span class="arrow">→</span> {esc(label[dst])}</h1>
<p class="soft">What CalSync copies from {esc(label[src])} onto {esc(label[dst])}. The other way has its own rule: <a href="/pair?{esc(other)}">{esc(label[dst])} → {esc(label[src])}</a>.</p>
<form method="post" action="/pair/save">{hidden_csrf(csrf)}<input type="hidden" name="src" value="{esc(src)}"><input type="hidden" name="dst" value="{esc(dst)}">
<section>{box("enabled", f"Copy events from {esc(label[src])} to {esc(label[dst])}", rule["enabled"], ' id="enabled"')}
<div id="keepbox"{"" if not rule["enabled"] else " hidden"}><fieldset><legend>The copies already made</legend>
<label class="check"><input type="radio" name="keep_copies" value=""{on(not rule["keep_copies"])}> Remove them at the next sync</label>
<label class="check"><input type="radio" name="keep_copies" value="1"{on(rule["keep_copies"])}> Keep them where they are. CalSync stops updating them.</label></fieldset></div></section>
<h2>Info to copy</h2><section><div class="two">
{box("copy_title", "Title", cp["title"], ' id="copy_title"')}{box("copy_location", "Location", cp["location"])}
{box("copy_description", "Description", cp["description"])}{box("copy_conference", "Video call link", cp["conference"])}
{box("copy_attendees", "Guest list, as text. Nobody is invited.", cp["attendees"])}{box("copy_reminders", "Reminders", cp["reminders"])}
{box("copy_private", "Mark copies private", cp["private"])}</div>
<label for="title_text">Without the title, copies say</label><input type="text" id="title_text" name="title_text" maxlength="60" value="{esc(cp["title_text"])}">
<p class="soft">A private event always shows only this, whatever is ticked.</p>
<label for="tag">Tag in front of the title</label><input type="text" id="tag" name="tag" maxlength="40" value="{esc(rule["tag"])}" placeholder="[{esc(label[src])}]">
<p class="sample">Copies look like: <b id="tagprev" data-title="{sample_title}">{esc(shown)}</b></p>
<label for="colour">Colour of the copies</label><select id="colour" name="colour"><option value="">The calendar's own colour</option><option value="match"{" selected" if rule["colour"] == "match" else ""}>Same as the original</option>{colours}</select></section>
<h2>Leave out</h2><section><div class="two">
{box("f_free", "Events marked free", f["free"])}{box("f_tentative", "Invites you answered Maybe", f["tentative"])}
{box("f_unanswered", "Invites you haven't answered", f["unanswered"])}{box("f_all_day", "All-day events", f["all_day"])}
{box("f_birthdays", "Birthdays", f["birthdays"])}{box("f_ooo", "Out of office", f["ooo"])}</div>
<fieldset><legend>Words in the title</legend><div class="inline-fields">{mode("f_keyword_mode", f["keyword_mode"])}<input type="text" name="f_keywords" value="{esc(", ".join(f["keywords"]))}" placeholder="lunch, gym" aria-label="Words, separated by commas"></div></fieldset>
<fieldset><legend>Colours</legend><div class="inline-fields">{mode("f_colour_mode", f["colour_mode"])}</div><div class="swatches">{swatch}</div><p class="soft">Google event colours and Outlook categories with these names.</p></fieldset>
<fieldset><legend>Only these days</legend><div class="days">{days}</div><p class="soft">None ticked means every day.</p></fieldset>
<fieldset><legend>Only events starting between</legend><div class="inline-fields"><input type="time" name="f_from" value="{esc(f["from"])}" aria-label="From"><span>and</span><input type="time" name="f_until" value="{esc(f["until"])}" aria-label="Until"></div><p class="soft">In {esc(engine.tz_name())}. All-day events skip this check.</p></fieldset></section>
<div class="actions"><button>Save</button><a class="button plain" href="/">Cancel</a></div></form>
<h2>Recent changes this way</h2><section><ul class="facts">{recent}</ul></section>'''
    return page("Sync rule", body, True, flash, at="/")


def rule_from_form(form):
    get = lambda k: form.get(k, [""])[0]
    return engine.clean_rule({
        "enabled": bool(get("enabled")), "keep_copies": bool(get("keep_copies")), "tag": get("tag").strip(), "colour": get("colour"),
        "copy": {k: bool(get("copy_" + k)) for k in engine.DEFAULT_COPY if k != "title_text"} | {"title_text": get("title_text")},
        "filters": {**{k: bool(get("f_" + k)) for k in ("free", "tentative", "unanswered", "all_day", "birthdays", "ooo")},
                    "colours": form.get("f_colours", []), "colour_mode": get("f_colour_mode"), "keywords": get("f_keywords"),
                    "keyword_mode": get("f_keyword_mode"), "days": form.get("f_days", []), "from": get("f_from"), "until": get("f_until")}})


def local_stamp(at):
    try:
        t = engine.dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=engine.dt.timezone.utc).astimezone(engine.local_tz())
    except (TypeError, ValueError):
        return at or ""
    return t.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def activity_page(csrf, flash=None):
    runs = engine.activity(100)
    trig = {"timer": "On schedule", "manual": "Sync now", "push": "A calendar changed", "check": "A calendar changed", "resync": "Resync", "quick": "Quick add", "start": "Started"}
    rows = []
    for r in runs:
        n = f'{r.get("created", 0)} added, {r.get("updated", 0)} updated, {r.get("deleted", 0)} removed'
        bad = r.get("errors") or r.get("failed")
        what = trig.get(r.get("trigger"), "Sync") + (f' ({r["pair"].replace(">", " → ")})' if r.get("pair") else "")
        lines = "\n".join(r.get("lines", []) + r.get("problems", []))
        rows.append(f'<details class="run"><summary><span class="who"><strong>{esc(local_stamp(r.get("at")))}</strong><span class="soft">{esc(what)} · {esc(n)}</span></span>'
                    f'<span class="st {"error" if bad else "synced"}">{"Problem" if bad else "OK"}</span></summary>'
                    + (f'<pre tabindex="0">{esc(lines)}</pre>' if lines else '<p class="soft pad">Nothing changed.</p>') + "</details>")
    body = "".join(rows) or '<p class="soft pad">No runs yet.</p>'
    return page("Activity", f'<h1>Activity</h1><p class="soft">Every sync, newest first. The last {engine.ACTIVITY_KEEP} are kept on this computer.</p><section class="runs">{body}</section>'
                f'<div class="actions"><a class="button plain" href="/">Back to sync</a></div>', True, flash, at="/")


def why_section(decisions, cfg, limit=300):
    """The Preview's answer to "why is or isn't this copied": every source event, where it goes, and the rule that decided."""
    if not decisions:
        return ""
    label = {c["name"]: c.get("label") or c["name"] for c in cfg["calendars"]}
    tz = engine.local_tz()
    by_event = {}
    for d in decisions:
        if d.get("action") == "remove":
            key = ("remove", d["dst"], d["title"], d["start"])
        else:
            key = (d["src"], d["title"], d["start"])
        by_event.setdefault(key, []).append(d)
    items = []
    for key, ds in sorted(by_event.items(), key=lambda kv: kv[0][-1])[:limit]:
        first = ds[0]
        try:
            day, _, when = _local(first, tz)
            when = f"{day.strftime('%a %b')} {day.day}, {when}"
        except (KeyError, ValueError):
            when = first["start"]
        title = "Busy" if first.get("private") else first["title"]
        if first.get("action") == "remove":
            head = f'<b>{esc(first["title"])}</b> <span class="soft">· copy on {esc(label.get(first["dst"], first["dst"]))} · {esc(when)}</span>'
            lis = f'<li class="no">To be removed: {esc(first["reason"])}.</li>'
        else:
            copied = sum(1 for d in ds if d["copied"])
            head = f'<b>{esc(title)}</b> <span class="soft">· {esc(label.get(first["src"], first["src"]))} · {esc(when)} · {"copied to " + str(copied) if copied else "not copied"}</span>'
            lis = "".join(f'<li class="{"yes" if d["copied"] else "no"}">' + (f'<b>{esc(label.get(d["dst"], d["dst"]))}</b>: ' if d["dst"] else "Everywhere: ")
                          + f'{"" if d["copied"] else "not copied, because "}{esc(d["reason"])}.</li>' for d in ds)
        items.append(f"<details><summary>{head}</summary><ul>{lis}</ul></details>")
    return f'<h2>Why each event is or isn\'t copied</h2><section class="why">{"".join(items)}</section>'


def remove_calendar_page(csrf, name):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    c = engine.calendar_named(name, cfg)
    lab = esc(c.get("label") or name)
    return page("Stop syncing", f'''<h1>Stop syncing {lab}?</h1><form method="post" action="/calendar/remove">{hidden_csrf(csrf)}<input type="hidden" name="name" value="{esc(name)}"><input type="hidden" name="confirm" value="1">
<section><fieldset><legend>The copies of {lab}'s events on your other calendars</legend>
<label class="check"><input type="radio" name="copies" value="remove" checked> Remove them</label>
<label class="check"><input type="radio" name="copies" value="keep"> Keep them. CalSync stops updating them, and they stay until you delete them.</label></fieldset>
<p class="soft">Copies CalSync made on {lab} itself stay either way. Delete them with "Delete the copies CalSync made here".</p></section>
<div class="actions"><button class="danger">Stop syncing {lab}</button><a class="button plain" href="/">Keep syncing</a></div></form>''', True, at="/")


# ---------- the Calendar tab: day, week and month, your own events, editing ----------

pending_ops = {}        # undo token -> a change waiting UNDO_SECONDS before it's written, or one just made that Undo reverses
UNDO_SECONDS = 10
_op_thread = {"on": False}


def calendar_prefs(cfg):
    v = cfg.get("view") or {}
    return {"hide_copies": bool(v.get("hide_copies")), "hide_weekends": bool(v.get("hide_weekends")),
            "week_numbers": bool(v.get("week_numbers")), "tz2": v.get("tz2") or ""}


def _zone(name):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def _range_for(view, anchor, prefs):
    D = engine.dt
    if view == "day":
        days = [anchor]
        return days, anchor - D.timedelta(days=1), anchor + D.timedelta(days=1), f"{anchor.strftime('%A, %B')} {anchor.day}, {anchor.year}"
    if view == "week":
        first = anchor - D.timedelta(days=(anchor.weekday() + 1) % 7)      # weeks start on Sunday
        days = [first + D.timedelta(days=i) for i in range(7)]
        if prefs["hide_weekends"]:
            days = [d for d in days if d.weekday() < 5]
        title = f"{days[0].strftime('%b')} {days[0].day} to {days[-1].strftime('%b')} {days[-1].day}, {days[-1].year}"
        if prefs["week_numbers"]:
            title += f" · Week {days[-1].isocalendar()[1]}"
        return days, first - D.timedelta(days=7), first + D.timedelta(days=7), title
    start = anchor.replace(day=1)
    first = start - D.timedelta(days=(start.weekday() + 1) % 7)
    nxt = (start + D.timedelta(days=32)).replace(day=1)
    count = (((nxt - D.timedelta(days=1)) - first).days // 7 + 1) * 7
    days = [first + D.timedelta(days=i) for i in range(count)]
    return days, (start - D.timedelta(days=1)).replace(day=1), nxt, start.strftime("%B %Y")


def _pending_for(cal, uid):
    now = time.time()
    with state_lock:
        return next((op for op in pending_ops.values() if op["kind"] in ("update", "delete") and op["cal"] == cal and op["uid"] == uid and op["due"] > now), None)


def gather(cfg, days, show="", fresh=False, hide_copies=False):
    """Every event to draw, as dicts with what the page needs. -> (by day, problems)."""
    D = engine.dt
    tz = engine.local_tz()
    cals = cfg["calendars"]
    label = {c["name"]: c.get("label") or c["name"] for c in cals}
    colour = colour_classes(cfg)
    lo = D.datetime.combine(days[0], D.time(), tz).astimezone(D.timezone.utc)
    hi = D.datetime.combine(days[-1] + D.timedelta(days=1), D.time(), tz).astimezone(D.timezone.utc)
    tmin, tmax = lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ")
    wanted = [c for c in cals if not show or c["name"] == show]
    results = {}
    threads = [threading.Thread(target=lambda c=c: results.__setitem__(c["name"], calendar_events(c, tmin, tmax, fresh))) for c in wanted]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    writable = {c["name"] for c in engine.writable_calendars(cfg)}
    skips = engine.skipped(cfg)
    per_day = {d: [] for d in days}
    seen, problems = set(), []
    for c in wanted:
        kind, evs = results.get(c["name"], ("err", "took too long to answer"))
        if kind == "err":
            problems.append(f"{label[c['name']]}: {evs}")
            continue
        for ev in evs:
            src = (ev.get("copy_of") or "").split("|")[0]
            if src and (not show or hide_copies):        # Everything: only real events; their copies are the same events again
                continue
            ident = ev.get("ical") or ev["uid"]
            if not show and (ident, ev["start"]) in seen:      # one meeting you were invited to on two calendars
                continue
            seen.add((ident, ev["start"]))
            op = _pending_for(c["name"], ev["uid"])
            if op and op["kind"] == "delete":
                continue
            ev = dict(ev)
            if op:
                ev.update({k: op["shown"][k] for k in ("title", "start", "end", "all_day") if k in op["shown"]})
            owner = src if src in colour else c["name"]
            orig = engine.original_of(ev["copy_of"], cfg) if src else None
            first_day, last_day, when = _local(ev, tz)
            item = dict(ev, cal=c["name"], owner=owner, k=colour[owner], copy=bool(src), who=label.get(owner, owner), when=when,
                        what="Busy" if ev.get("private") and src else (ev.get("title") or "(no title)"),
                        key=ev["copy_of"] if src else f"{c['name']}|{ev['uid']}", pend=bool(op),
                        link_cal=orig[0]["name"] if orig else c["name"], link_uid=orig[1] if orig else ev["uid"],
                        movable="" if src or c["name"] not in writable else ("ask" if ev.get("series") or ev.get("attendees") else "1"))
            item["gone"] = item["key"] in skips
            d = max(first_day, days[0])
            while d <= min(last_day, days[-1]) and d - first_day < D.timedelta(days=62):
                if d in per_day:
                    per_day[d].append(item)
                d += D.timedelta(days=1)
    return per_day, problems


def event_link(item, day):
    q = {"cal": item["link_cal"], "uid": item["link_uid"], "d": day.isoformat()}
    if item.get("href") and not item["copy"]:
        q["href"] = item["href"]
    return "/event?" + urllib.parse.urlencode(q)


def _minutes(ev, day, tz):
    D = engine.dt
    s = D.datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    e = D.datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    day0 = D.datetime.combine(day, D.time(), tz)
    a = max(0, int((s - day0).total_seconds() // 60))
    b = min(1440, int((e - day0).total_seconds() // 60))
    return a, max(b, a + 15)


def _columns(spans):
    """Side-by-side columns for overlapping events: -> [(column, columns in its group)], at most 4 across."""
    out, group, ends, group_end = [None] * len(spans), [], [], 0

    def close():
        n = min(4, len(ends))
        for j in group:
            out[j] = (min(out[j][0], n - 1), n)
    for i in sorted(range(len(spans)), key=lambda i: spans[i]):
        a, b = spans[i]
        if group and a >= group_end:
            close()
            group, ends, group_end = [], [], 0
        col = next((k for k, e in enumerate(ends) if e <= a), None)
        if col is None:
            col = len(ends)
            ends.append(b)
        else:
            ends[col] = b
        out[i] = (col, 0)
        group.append(i)
        group_end = max(group_end, b)
    if group:
        close()
    return out


def chip_link(item, day, timed_class=""):
    cls = f'ev {item["k"]}{" copy" if item["copy"] else ""}{" gone" if item["gone"] else ""}{" pend" if item["pend"] else ""} {timed_class}'.strip()
    data = ""
    if item["movable"] and timed_class:
        data = (f' data-move="{item["movable"]}" data-cal="{esc(item["cal"])}" data-uid="{esc(item["uid"])}"'
                + (f' data-href="{esc(item["href"])}"' if item.get("href") else ""))
    tip = f'{item["who"]}{" (copy)" if item["copy"] else ""}{" (skipped)" if item["gone"] else ""}'
    grip = '<span class="rs" aria-hidden="true"></span>' if data else ""
    return (f'<div class="{cls}"{data} title="{esc(tip)}"><a class="evl" href="{esc(event_link(item, day))}"><span class="t">{esc(item["when"])}</span>{esc(item["what"])}</a>{grip}</div>')


def calendar_page(csrf, q, flash=None, quick=None):
    D = engine.dt
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    cals = cfg["calendars"]
    if not cals:
        return page("Calendar", '<h1>Calendar</h1><section class="empty"><p>Add a calendar first, and its events show up here.</p><a class="button" href="/#add">Add a calendar</a></section>', True, flash, at="/calendar")
    tz = engine.local_tz()
    today = D.datetime.now(tz).date()
    prefs = calendar_prefs(cfg)
    view = q.get("view", [""])[0]
    view = view if view in ("day", "week", "month") else "week"
    try:
        anchor = D.date.fromisoformat(q.get("d", [""])[0])
    except ValueError:
        anchor = today
    names = [c["name"] for c in cals]
    show = q.get("show", [""])[0]
    show = show if show in names else ""
    label = {c["name"]: c.get("label") or c["name"] for c in cals}
    colour = colour_classes(cfg)
    days, back, fwd, title = _range_for(view, anchor, prefs)
    per_day, problems = gather(cfg, days, show, q.get("fresh", [""])[0] == "1", prefs["hide_copies"])
    if view == "month" and prefs["hide_weekends"]:
        days = [d for d in days if d.weekday() < 5]

    def link(**kw):
        p = {"view": view, "d": anchor.isoformat(), "show": show}
        p.update(kw)
        return "/calendar?" + urllib.parse.urlencode({k: v for k, v in p.items() if v})

    seg = "".join(f'<a href="{esc(link(view=v))}"{" aria-current=page" if v == view else ""} data-{v}>{v.title()}</a>' for v in ("day", "week", "month"))
    step = (f'<a href="{esc(link(d=back.isoformat()))}" aria-label="Earlier" data-prev>&lsaquo;</a><a href="{esc(link(d=today.isoformat()))}" data-today>Today</a>'
            f'<a href="{esc(link(d=fwd.isoformat()))}" aria-label="Later" data-next>&rsaquo;</a>')
    new_at = "/event/new?" + urllib.parse.urlencode({"d": anchor.isoformat(), "cal": show})
    search = f'<form method="get" action="/search" role="search"><label for="search" class="vh">Search</label><input type="search" id="search" name="q" placeholder="Search" maxlength="200"></form>'
    legend = [f'<a href="{esc(link(show=""))}"{" aria-current=page" if not show else ""}>Everything</a>']
    legend += [f'<a class="{colour[n]}" href="{esc(link(show=n))}"{" aria-current=page" if show == n else ""}><span class="dot"></span>{esc(label[n])}</a>' for n in names]
    toggle = lambda key, text: (f'<form method="post" action="/calendar/prefs">{hidden_csrf(csrf)}<input type="hidden" name="pref" value="{key}"><input type="hidden" name="back" value="{esc(link())}">'
                                f'<button aria-pressed="{"true" if prefs[key] else "false"}">{text}</button></form>')
    toggles = (f'<div class="toggles">{toggle("hide_copies", "Hide copies")}{toggle("hide_weekends", "Hide weekends")}{toggle("week_numbers", "Week numbers")}'
               f'<a class="button plain" href="{esc(new_at)}" data-new>New event</a><button class="plain" data-print hidden>Print</button></div>')
    if view == "month":
        grid = month_grid(days, per_day, anchor, today, prefs, link)
    else:
        grid = time_grid(days, per_day, today, prefs, csrf, show)
    if show:
        explain = (f"What is on {esc(label[show])} right now. Its own events are solid; striped ones are copies CalSync made, in the colour of the calendar they came from. "
                   + ("Copies are hidden. " if prefs["hide_copies"] else ""))
    else:
        explain = "Your own events from every calendar, one colour each. Once synced, every calendar CalSync writes to holds all of these. "
    bad = "".join(f"<li>{esc(p)}</li>" for p in problems)
    bad = f'<section class="bad"><p>Some calendars couldn\'t be read just now:</p><ul>{bad}</ul></section>' if bad else ""
    writable = engine.writable_calendars(cfg)
    try:
        main = engine.main_calendar(cfg)["name"] if writable else None
    except SystemExit:
        main = None
    pick = q.get("cal", [main or ""])[0]
    opts = "".join(f'<option value="{esc(c["name"])}"{" selected" if c["name"] == pick else ""}>{esc(c.get("label") or c["name"])}</option>' for c in writable)
    adder = (f'''<form class="quick" method="post" action="/quick" autocomplete="off">{hidden_csrf(csrf)}<input type="hidden" name="back" value="{esc(link())}">
<label for="qa" class="vh">Add an event</label><input type="text" id="qa" name="text" required maxlength="300" value="{esc(q.get("text", [""])[0])}" placeholder="Breakfast with friends tomorrow 9am for 2 hours">
<select name="calendar" aria-label="Add it to">{opts}</select><button>Preview</button></form>''' if writable else "")
    skips = engine.skipped(cfg)
    gone = [x for x in cfg.get("skips", []) if x["key"] in skips]
    skipped_list = ('<h2>Skipped</h2><section class="list">' + "".join(
        f'''<div class="row"><div><b>{esc(x["title"])}</b><span class="soft">{esc(x["start"][:10])} · not copied, so it doesn't block your other calendars</span></div>
<form class="inline" method="post" action="/unskip">{hidden_csrf(csrf)}<input type="hidden" name="key" value="{esc(x["key"])}"><input type="hidden" name="back" value="{esc(link())}"><button class="plain">Undo</button></form></div>'''
        for x in gone) + "</section>") if gone else ""
    tz2 = f" and {esc(prefs['tz2'])}" if prefs["tz2"] and view != "month" else ""
    keys = ('<p class="keys noprint" id="keys" hidden><kbd>D</kbd> day · <kbd>W</kbd> week · <kbd>M</kbd> month · <kbd>T</kbd> today · <kbd>J</kbd>/<kbd>K</kbd> next and back · '
            '<kbd>N</kbd> new event · <kbd>/</kbd> search · <kbd>P</kbd> print · <kbd>Esc</kbd> close</p>')
    body = f'''<div class="calbar" data-cal-page><h1>Calendar</h1><div class="tools">{search}<nav class="seg" aria-label="View">{seg}</nav><span class="step">{step}</span></div></div>
{adder}{quick or ""}{undo_toast(csrf, q)}<p class="range">{esc(title)}</p><div class="legend" role="group" aria-label="Calendars">{"".join(legend)}</div>{toggles}{bad}
{grid}
<p class="calnote">{explain}Open an event to see it, change it, answer it, or skip copying it. Drag one to move it; drag its bottom edge to make it longer. Double-click an empty time to add one there. Times are in {esc(engine.tz_name())}{tz2}. Press <kbd>?</kbd> for keys. Read a moment ago; <a href="{esc(link(fresh="1"))}">refresh</a>.</p>{keys}{skipped_list}'''
    return page("Calendar", body, True, flash, at="/calendar", wide=True)


def month_grid(days, per_day, anchor, today, prefs, link):
    cols = 5 if prefs["hide_weekends"] else 7
    names = ("Mon", "Tue", "Wed", "Thu", "Fri") if prefs["hide_weekends"] else ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
    cells = [f'<div class="wd">{w}</div>' for w in names]
    for i, d in enumerate(days):
        items = sorted(per_day.get(d, []), key=lambda it: (not it["all_day"], it["start"], it["what"]))
        cls = " ".join(x for x in ("today" if d == today else "", "other" if d.month != anchor.month else "") if x)
        wk = f'<span class="wkn">W{d.isocalendar()[1]}</span>' if prefs["week_numbers"] and i % cols == 0 else ""
        head = f'<p class="dh"><a class="dn" href="{esc(link(view="day", d=d.isoformat()))}">{d.day}</a>{wk}</p>'
        evs = "".join(chip_link(it, d) for it in items[:3])
        if len(items) > 3:
            evs += f'<a class="plus" href="{esc(link(view="day", d=d.isoformat()))}">{len(items) - 3} more</a>'
        cells.append(f'<div{" class=" + chr(34) + cls + chr(34) if cls else ""}>{head}{evs}</div>')
    return f'<div class="grid month{" nowe" if prefs["hide_weekends"] else ""}">{"".join(cells)}</div>'


def time_grid(days, per_day, today, prefs, csrf, show):
    D = engine.dt
    tz = engine.local_tz()
    tz2 = _zone(prefs["tz2"])
    n = len(days)
    zones = (f'<div class="zones">{esc(prefs["tz2"].split("/")[-1].replace("_", " "))} · {esc(engine.tz_name().split("/")[-1].replace("_", " "))}</div>' if tz2 else '<div></div>')
    head = [zones] + [f'<div class="{"today" if d == today else ""}"><span class="wn">{d.strftime("%a")}</span> <span class="dn">{d.day}</span>'
                               + (f' <span class="wkn">W{d.isocalendar()[1]}</span>' if prefs["week_numbers"] and (i == 0) else "") + "</div>" for i, d in enumerate(days)]
    alls = ['<div>all day</div>']
    cols = []
    for d in days:
        items = per_day.get(d, [])
        alls.append("<div>" + "".join(chip_link(it, d) for it in sorted((i for i in items if i["all_day"]), key=lambda it: it["what"])) + "</div>")
        timed = [i for i in items if not i["all_day"]]
        spans = [_minutes(i, d, tz) for i in timed]
        places = _columns(spans)
        evs = []
        for it, (a, b), (col, of) in zip(timed, spans, places):
            cls = f"s{min(a // 15, 95)} l{max(1, min(96, -(-(b - a) // 15)))}" + (f" c{col}-{of}" if of > 1 else "")
            evs.append(chip_link(it, d, cls))
        now = ""
        if d == today:
            t = D.datetime.now(tz)
            now = f'<div class="now s{(t.hour * 60 + t.minute) // 15}" aria-hidden="true"></div>'
        new = "/event/new?" + urllib.parse.urlencode({"d": d.isoformat(), "cal": show})
        cols.append(f'<div class="tg-day{" today" if d == today else ""}{" wk" if d.weekday() >= 5 else ""}" data-date="{d.isoformat()}" data-new="{esc(new)}">{now}{"".join(evs)}</div>')
    hours = []
    for h in range(24):
        here = D.datetime.combine(days[0], D.time(h), tz)
        lab = here.strftime("%I %p").lstrip("0") if h else ""
        if tz2:
            other = here.astimezone(tz2).strftime("%I %p").lstrip("0")
            hours.append(f"<span>{other if h else ''}</span><span>{lab}</span>")
        else:
            hours.append(f"<span>{lab}</span>")
    gut = f'<div class="tg-hours{" two" if tz2 else ""}">{"".join(hours)}</div>'
    cls = f'tg d{n if n in (1, 5, 7) else 7}{" tz2" if tz2 else ""}'
    return (f'<div class="{cls}"><div class="tg-head">{"".join(head)}</div><div class="tg-all">{"".join(alls)}</div>'
            f'<div class="tg-scroll"><div class="tg-body" data-csrf="{csrf}">{gut}{"".join(cols)}</div></div></div>')


def undo_toast(csrf, q):
    token = q.get("undo", [""])[0]
    with state_lock:
        op = pending_ops.get(token)
    if not op or op["undo_until"] < time.time():
        return ""
    return (f'<div class="toast" role="status" data-until="{int(op["undo_until"] * 1000)}">{esc(op["said"])} <span class="soft left"></span>'
            f'<form method="post" action="/undo">{hidden_csrf(csrf)}<input type="hidden" name="token" value="{esc(token)}"><input type="hidden" name="back" value="{esc(back_to(op["back"]))}"><button>Undo</button></form></div>')


def find_event(c, uid, day):
    """One event on one calendar, from the listing of the days around it (the same cached read the Calendar tab uses)."""
    D = engine.dt
    tz = engine.local_tz()
    lo = D.datetime.combine(day - D.timedelta(days=1), D.time(), tz).astimezone(D.timezone.utc)
    hi = D.datetime.combine(day + D.timedelta(days=2), D.time(), tz).astimezone(D.timezone.utc)
    kind, evs = calendar_events(c, lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if kind == "err":
        raise RuntimeError(evs)
    return next((e for e in evs if e["uid"] == uid), None)


def _local_parts(ev, tz):
    D = engine.dt
    if ev["all_day"]:
        s = D.date.fromisoformat(ev["start"][:10])
        e = D.date.fromisoformat(ev["end"][:10]) - D.timedelta(days=1)
        return s.isoformat(), "09:00", max(e, s).isoformat(), "10:00"
    s = D.datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    e = D.datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=D.timezone.utc).astimezone(tz)
    return s.date().isoformat(), s.strftime("%H:%M"), e.date().isoformat(), e.strftime("%H:%M")


def event_form_fields(ev, cfg, creating, cal_pick=""):
    """The editor's fields. ev is None for a new event."""
    tz = engine.local_tz()
    on = lambda v: " checked" if v else ""
    ev = ev or {}
    date, start, end_date, end = ev.get("parts") or ("", "09:00", "", "10:00")
    colours = "".join(f'<option value="{k}"{" selected" if (ev.get("colours") or [""])[0] == k else ""}>{k.title()}</option>' for k in engine.PALETTE)
    out = [f'<label for="title">Title</label><input type="text" id="title" name="title" maxlength="300" value="{esc(ev.get("title", ""))}" required autofocus>']
    if creating:
        writable = engine.writable_calendars(cfg)
        opts = "".join(f'<option value="{esc(c["name"])}"{" selected" if c["name"] == cal_pick else ""}>{esc(c.get("label") or c["name"])}</option>' for c in writable)
        out.append(f'<label for="cal">Calendar</label><select id="cal" name="cal">{opts}</select>')
    out.append(f'''<fieldset><legend>When</legend><div class="inline-fields"><input type="date" name="date" value="{esc(date)}" required aria-label="Start date">
<input type="time" name="start" value="{esc(start)}" aria-label="Start time"><span>to</span><input type="date" name="end_date" value="{esc(end_date)}" aria-label="End date"><input type="time" name="end" value="{esc(end)}" aria-label="End time"></div>
<label class="check"><input type="checkbox" name="all_day" value="1"{on(ev.get("all_day"))}> All day</label><p class="soft">In {esc(engine.tz_name())}.</p></fieldset>''')
    if creating or not ev.get("series"):
        reps = "".join(f'<option value="{k}">{v}</option>' for k, v in engine.REPEATS.items())
        out.append(f'<label for="repeat">Repeat</label><div class="inline-fields"><select id="repeat" name="repeat">{reps}</select><input type="date" name="until" aria-label="Repeat until (optional)"></div><p class="soft">Leave the date empty to repeat with no end.</p>')
    out.append(f'<label for="location">Location</label><input type="text" id="location" name="location" maxlength="500" value="{esc(ev.get("location", ""))}">')
    if creating:
        out.append('<label for="conference">Video call</label><select id="conference" name="conference"><option value="">None</option><option value="meet">Google Meet (Google calendars)</option>'
                   '<option value="teams">Microsoft Teams (work or school Microsoft calendars)</option></select><p class="soft">For Zoom or anything else, paste the link into Location.</p>')
    out.append(f'''<div class="two"><label class="check"><input type="checkbox" name="busy" value="1"{on(ev.get("busy", True))}> Shows as busy</label>
<label class="check"><input type="checkbox" name="private" value="1"{on(ev.get("private"))}> Private</label></div>
<label for="colour">Colour</label><select id="colour" name="colour"><option value="">The calendar's colour</option>{colours}</select>
<label for="guests">Guests</label><textarea id="guests" name="guests" rows="2" placeholder="sam@example.com, alex@example.com">{esc(", ".join(ev.get("attendees") or []))}</textarea>
<label for="description">Description</label><textarea id="description" name="description" maxlength="{engine.MAX_TEXT}">{esc(ev.get("description", ""))}</textarea>''')
    return "".join(out)


def scope_fields(ev, verb="Change"):
    if not ev.get("series"):
        return ""
    return (f'<fieldset><legend>This repeats. {verb}:</legend><label class="check"><input type="radio" name="scope" value="this" checked> Only this one</label>'
            '<label class="check"><input type="radio" name="scope" value="following"> This one and the ones after it</label>'
            '<label class="check"><input type="radio" name="scope" value="all"> Every one</label></fieldset>')


def notify_field(ev):
    if not ev.get("attendees"):
        return ""
    return '<label class="check"><input type="checkbox" name="notify" value="1" checked> Email the guests about this</label>'


def event_page(csrf, q, flash=None):
    D = engine.dt
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    tz = engine.local_tz()
    name, uid = q.get("cal", [""])[0], q.get("uid", [""])[0]
    back = back_to(q.get("back", [""])[0] or "/calendar?" + urllib.parse.urlencode({"view": "week", "d": q.get("d", [""])[0]}))
    try:
        day = D.date.fromisoformat(q.get("d", [""])[0])
    except ValueError:
        day = D.datetime.now(tz).date()
    try:
        c = engine.calendar_named(name, cfg)
        ev = find_event(c, uid, day)
    except Exception as e:
        return page("Event", f'<h1>Couldn\'t open that event</h1><section class="bad">{esc(short_error(e))}</section><a class="button plain" href="{esc(back)}" data-close>Back</a>', True, at="/calendar")
    note = ""
    if ev and ev.get("copy_of"):            # a copy: open its original instead, which is what an edit should change
        orig = engine.original_of(ev["copy_of"], cfg)
        if orig:
            try:
                o = find_event(orig[0], orig[1], day)
            except Exception:
                o = None
            if o:
                note = f'<section class="note">You opened the copy on {esc(c.get("label") or name)}. This is the original on {esc(orig[0].get("label") or orig[0]["name"])}; changes here reach every copy at the next sync.</section>'
                c, ev, uid = orig[0], o, orig[1]
    if not ev:
        return page("Event", f'<h1>That event isn\'t there any more</h1><section class="note">It may have moved or been deleted. Refresh the calendar.</section><a class="button plain" href="{esc(back)}" data-close>Back</a>', True, at="/calendar")
    lab = c.get("label") or c["name"]
    first, _, when = _local(ev, tz)
    parts = _local_parts(ev, tz)
    if q.get("start") and q.get("date"):       # a drag that needs a yes (repeating, or has guests): its new time comes prefilled
        parts = (q["date"][0], q["start"][0], q["date"][0], q.get("end", [q["start"][0]])[0])
    shown = dict(ev, parts=parts)
    writable = c["name"] in {x["name"] for x in engine.writable_calendars(cfg)}
    editable = writable and ev.get("me_organizer", True) and not ev.get("copy_of")
    facts = [f'<li>{esc(first.strftime("%A, %B"))} {first.day}, {esc(when)}{" · repeats" if ev.get("series") else ""}</li>', f'<li>On <b>{esc(lab)}</b></li>']
    if ev.get("location"):
        facts.append(f'<li>{esc(ev["location"])}</li>')
    if ev.get("organizer") and not ev.get("me_organizer", True):
        facts.append(f'<li>From {esc(ev["organizer"])}</li>')
    if ev.get("attendees"):
        facts.append(f'<li>Guests: {esc(", ".join(ev["attendees"][:30]))}</li>')
    if not ev.get("busy", True):
        facts.append("<li>Shows as free</li>")
    if ev.get("description"):
        facts.append(f'<li><pre tabindex="0">{esc(ev["description"][:4000])}</pre></li>')
    ident = (hidden_csrf(csrf) + f'<input type="hidden" name="cal" value="{esc(c["name"])}"><input type="hidden" name="uid" value="{esc(uid)}">'
             f'<input type="hidden" name="href" value="{esc(ev.get("href") or "")}"><input type="hidden" name="back" value="{esc(back)}"><input type="hidden" name="d" value="{esc(first.isoformat())}">')
    buttons = []
    if ev.get("conference"):
        buttons.append(f'<a class="button" href="{esc(ev["conference"])}" target="_blank" rel="noreferrer noopener">Join</a>')
    if ev.get("rsvp"):
        cur = ev["rsvp"]
        buttons.append(f'<form class="rsvp" method="post" action="/event/rsvp">{ident}' + "".join(
            f'<button class="plain" name="answer" value="{a}" aria-pressed="{"true" if cur == a else "false"}">{t}</button>' for a, t in (("yes", "Going"), ("maybe", "Maybe"), ("no", "No")))
            + "</form>")
    key = f'{c["name"]}|{uid}'
    skipping = key in engine.skipped(cfg)
    skip = (f'<form class="inline" method="post" action="{"/unskip" if skipping else "/skip"}">{hidden_csrf(csrf)}<input type="hidden" name="key" value="{esc(key)}"><input type="hidden" name="back" value="{esc(back)}">'
            f'<input type="hidden" name="title" value="{esc(ev.get("title") or "")}"><input type="hidden" name="start" value="{esc(ev["start"])}">'
            f'<button class="plain">{"Copy it again" if skipping else "Skip copying this one"}</button></form>')
    head = f'<div class="evhead"><h1>{esc(ev.get("title") or "(no title)")}</h1></div>{note}<section><ul class="facts">{"".join(facts)}</ul><div class="actions">{"".join(buttons)}{skip}</div></section>'
    if not editable:
        why = ("This is an invite from someone else, so change it where it was sent from. You can still answer it here." if writable and not ev.get("me_organizer", True)
               else f"{esc(lab)} is read only in CalSync, so change this event where it lives.")
        return page("Event", f'{head}<section class="note">{why}</section><div class="actions"><a class="button plain" href="{esc(back)}" data-close>Back</a></div>', True, flash, at="/calendar")
    form = (f'<h2>Change it</h2><form method="post" action="/event/save"><section>{ident}{event_form_fields(shown, cfg, False)}{scope_fields(ev)}{notify_field(ev)}'
            f'<div class="actions"><button>Save</button><a class="button plain" href="{esc(back)}" data-close>Cancel</a></div></section></form>'
            f'<h2>Delete it</h2><form method="post" action="/event/delete"><section>{ident}{scope_fields(ev, "Delete")}{notify_field(ev)}'
            f'<div class="actions"><button class="danger">Delete</button></div></section></form>')
    return page("Event", head + form, True, flash, at="/calendar")


def new_event_page(csrf, q, flash=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    writable = engine.writable_calendars(cfg)
    if not writable:
        return page("New event", '<h1>New event</h1><section class="note">None of your calendars can be written to from CalSync yet.</section>', True, flash, at="/calendar")
    D = engine.dt
    date = q.get("d", [""])[0] or D.datetime.now(engine.local_tz()).date().isoformat()
    t = q.get("t", ["09:00"])[0]
    try:
        h, m = (int(x) for x in t.split(":"))
        end = f"{min(23, h + 1):02d}:{m:02d}"
    except ValueError:
        t, end = "09:00", "10:00"
    pick = q.get("cal", [""])[0]
    if pick not in {c["name"] for c in writable}:
        try:
            pick = engine.main_calendar(cfg)["name"]
        except SystemExit:
            pick = writable[0]["name"]
    back = back_to("/calendar?" + urllib.parse.urlencode({"view": "week", "d": date}))
    fields = event_form_fields({"parts": (date, t, date, end), "busy": True}, cfg, True, pick)
    return page("New event", f'''<h1>New event</h1><form method="post" action="/event/create"><section>{hidden_csrf(csrf)}<input type="hidden" name="back" value="{esc(back)}">{fields}
<label class="check"><input type="checkbox" name="notify" value="1" checked> Email the invites to the guests</label>
<div class="actions"><button>Add it</button><a class="button plain" href="{esc(back)}" data-close>Cancel</a></div></section></form>
<p class="soft">It goes on the calendar you pick, then CalSync copies it to your other calendars by your sync rules.</p>''', True, flash, at="/calendar")


def event_from_form(get, form):
    """The editor's form as the engine's raw event."""
    D = engine.dt
    tz = engine.local_tz()
    date, end_date = get("date"), get("end_date") or get("date")
    all_day = bool(get("all_day"))
    raw = {"title": get("title"), "all_day": all_day, "location": get("location"), "description": get("description"),
           "busy": bool(get("busy")), "private": bool(get("private")), "colour": get("colour"), "guests": get("guests")}
    if all_day:
        raw.update(start=date, end=end_date, end_inclusive=True)
    else:
        s = D.datetime.fromisoformat(f"{date}T{get('start') or '09:00'}").replace(tzinfo=tz)
        e = D.datetime.fromisoformat(f"{end_date}T{get('end') or get('start') or '10:00'}").replace(tzinfo=tz)
        if e <= s and not get("end_date"):
            e += D.timedelta(days=1)
        raw.update(start=s, end=e)
    if get("repeat") in engine.REPEATS and get("repeat"):
        raw["repeat"] = engine.repeat_rule(get("repeat"), raw["start"], get("until") or None)
    if get("conference"):
        raw["conference"] = get("conference")
    return raw


def queue_op(kind, cal, uid, changes, scope, notify, href, said, back, shown=None):
    """Hold a change for UNDO_SECONDS so Undo can simply drop it; then write it."""
    token = secrets.token_urlsafe(12)
    now = time.time()
    with state_lock:
        pending_ops[token] = {"kind": kind, "cal": cal, "uid": uid, "changes": changes, "scope": scope, "notify": notify, "href": href,
                              "said": said, "back": back, "shown": shown or {}, "due": now + UNDO_SECONDS, "undo_until": now + UNDO_SECONDS}
        start = not _op_thread["on"]
        _op_thread["on"] = True
    if start:
        threading.Thread(target=op_runner, daemon=True).start()
    return token


def op_runner():
    while True:
        time.sleep(.5)
        now = time.time()
        with state_lock:
            due = [(t, op) for t, op in pending_ops.items() if op["kind"] in ("update", "delete") and op["due"] <= now and not op.get("done")]
            for t, op in due:
                op["done"] = True
            for t in [t for t, op in pending_ops.items() if op["undo_until"] < now - 60]:
                del pending_ops[t]
        for t, op in due:
            run_op(op)


def run_op(op):
    try:
        if op["kind"] == "update":
            engine.event_update(op["cal"], op["uid"], op["changes"], op["scope"], op["notify"], op["href"] or None)
        else:
            engine.event_delete(op["cal"], op["uid"], op["scope"], op["notify"], op["href"] or None)
    except Exception as e:
        with state_lock:
            op_errors.append(f"{op['said'].rstrip('.')} didn't work: {short_error(e)}")
            del op_errors[:-5]
    with state_lock:
        CAL_CACHE.clear()
    if engine.load_json(engine.CONFIG, {}).get("enabled"):
        run_in_background(trigger="manual")


op_errors = []


def search_page(csrf, q):
    text = q.get("q", [""])[0][:200]
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    rows, problems = engine.search_events(text, cfg) if text.strip() else ([], [])
    tz = engine.local_tz()
    colour = colour_classes(cfg)
    label = {c["name"]: c.get("label") or c["name"] for c in cfg["calendars"]}
    out = []
    for ev in rows:
        day, _, when = _local(ev, tz)
        link = "/event?" + urllib.parse.urlencode({"cal": ev["calendar"], "uid": ev["uid"], "d": day.isoformat(), **({"href": ev["href"]} if ev.get("href") else {})})
        out.append(f'<div class="row"><span class="when">{esc(day.strftime("%a %b"))} {day.day}, {day.year}<br>{esc(when)}</span><div><a href="{esc(link)}"><b>{esc(ev.get("title") or "(no title)")}</b></a>'
                   f'<span class="soft"><span class="dot {colour.get(ev["calendar"], "k0")}"></span> {esc(label.get(ev["calendar"], ev["calendar"]))}{" · " + esc(ev["location"]) if ev.get("location") else ""}</span></div></div>')
    bad = f'<section class="bad"><p>Some calendars couldn\'t be searched just now:</p><ul>{"".join(f"<li>{esc(p)}</li>" for p in problems)}</ul></section>' if problems else ""
    found = (f'<section class="list results">{"".join(out)}</section>' if out else
             ('<section class="note">Nothing matches.</section>' if text.strip() else ""))
    return page("Search", f'''<h1>Search</h1><form method="get" action="/search" role="search"><div class="quick"><label for="search" class="vh">Search</label>
<input type="search" id="search" name="q" value="{esc(text)}" maxlength="200" placeholder="dentist cal:personal before:2027-01-01" autofocus><button>Search</button></div></form>
<p class="soft">Searches titles, places, notes and guests on every calendar, 90 days back and a year ahead. Narrow it with <code>cal:name</code>, <code>from:someone@example.com</code>, <code>before:2026-12-31</code> and <code>after:2026-12-01</code>.</p>
{bad}{found}<div class="actions"><a class="button plain" href="/calendar" data-close>Back to the calendar</a></div>''', True, at="/calendar")


# ---------- background sync ----------

def run_in_background(force=False, pair=None, refresh=False, trigger="timer"):
    with state_lock:
        if run_state["running"]:
            return False
        run_state["running"] = True

    def job():
        try:
            engine.run_once(force=force, log=lambda line: None, pair=pair, refresh=refresh, trigger=trigger)     # titles stay out of the container's logs
        except Exception as e:
            engine.save_json(engine.LASTRUN, {"at": engine.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": 1, "failed": [], "problems": [f"! {short_error(e)}"]})
        finally:
            with state_lock:
                run_state["running"] = False
    threading.Thread(target=job, daemon=True).start()
    return True


pushed = threading.Event()      # set when Google or Microsoft says a calendar changed
QUICK_CHECK_SECONDS = 60


def push_file():
    return os.path.join(engine.SECRETS, "push.json")


def push_words():
    if PUBLIC_URL.startswith("https://"):
        return f"CalSync has a public address ({PUBLIC_URL}), so Google and Microsoft tell it the moment a calendar changes. iCloud, other CalDAV and .ics links sync on the interval."
    return ("Instant updates from Google and Microsoft need a public https address (CALSYNC_PUBLIC_URL), since they call CalSync when something changes. "
            "Without one, the one-minute check above is nearly as quick. iCloud, other CalDAV and .ics links sync on the interval.")


def keep_push(cfg):
    """With a public https address: make sure every Google and Microsoft calendar has a live subscription, renewing
    each a day before it runs out. Problems are kept quiet; the one-minute check still runs."""
    if not PUBLIC_URL.startswith("https://") or not cfg.get("enabled"):
        return
    state = engine.load_json(push_file(), {}) or {}
    state.setdefault("secret", secrets.token_urlsafe(24))
    subs = state.setdefault("subs", {})
    now = time.time()
    for c in cfg["calendars"]:
        try:
            ad = engine.make_adapter(c)
        except Exception:
            continue
        if not hasattr(ad, "watch"):
            continue
        key = f'{c["name"]}|{c.get("calendar_id")}'
        have = subs.get(key)
        if have and have.get("expires", 0) - now > 86400:
            continue
        kind = "google" if isinstance(ad, engine.GoogleCalendar) else "microsoft"
        try:
            subs[key] = dict(ad.watch(f"{PUBLIC_URL}/hook/{kind}", state["secret"], secrets.token_hex(16)), kind=kind)
            if have:
                try:
                    ad.unwatch(have)
                except Exception:
                    pass
        except Exception:
            continue
    for key in [k for k in subs if k.split("|")[0] not in {c["name"] for c in cfg["calendars"]}]:
        subs.pop(key)
    engine.save_json(push_file(), state)


def scheduler(stop):
    last = last_check = last_push = 0.0
    checked_at = engine.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    while not stop.wait(5):
        cfg = engine.load_json(engine.CONFIG, {"calendars": []})
        if not cfg.get("enabled"):
            continue
        every = cfg.get("interval_minutes", 5) * 60
        now = time.time()
        if pushed.is_set() and now - last > 10 and run_in_background(trigger="push"):      # many pushes close together make one run
            pushed.clear()
            last = now
            continue
        if now - last >= every and run_in_background():
            last = now
            checked_at = engine.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
            continue
        if cfg.get("fast_check", True) and now - last_check >= QUICK_CHECK_SECONDS and now - last > 30:
            last_check = now
            since, checked_at = checked_at, engine.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
            if engine.changed_since(cfg, since) and run_in_background(trigger="check"):
                last = time.time()
        if now - last_push > 3600:
            last_push = now
            threading.Thread(target=keep_push, args=(cfg,), daemon=True).start()


def tidy_flows():
    now = time.time()
    with state_lock:
        for flows in (pending_google, pending_ms, pending_oauth):
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
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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
        here.host = self.headers.get("Host")
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":     # before the host check, so a container's health probe always works
            return self.send(200, "ok", "text/plain")
        if not host_allowed(self.headers.get("Host")):
            return self.send(400, "This host name isn't allowed. Add it to CALSYNC_HOSTS.", "text/plain")
        if url.path == "/app.css":
            return self.send(200, CSS, "text/css")
        if url.path == "/app.js":
            return self.send(200, JS, "text/javascript")
        if url.path == "/" and ("state" in q or "code" in q or "error" in q):
            with state_lock:
                mine = pending_oauth.get(q.get("state", [""])[0])
            if mine:        # one-click Microsoft comes back to "/" (its registered address has no path)
                return self.oauth_callback(mine["provider"], q)
            return self.google_callback(q)
        if url.path in ("/oauth/google", "/oauth/microsoft"):
            return self.oauth_callback(url.path.rsplit("/", 1)[1], q)
        cfg = web_config()
        if not cfg:
            return self.send(200, self.first_run_page())
        if url.path == "/forgot":
            return self.send(200, self.forgot_page())
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
        if url.path == "/connect":      # old links: adding a calendar is on the sync page now
            return self.redirect("/" + ("?m=" + q["m"][0] if q.get("m", [""])[0] in FLASHES else "") + "#add")
        if url.path == "/connect/google":
            return self.send(200, google_page(csrf, flash))
        if url.path == "/connect/advanced":
            return self.send(200, advanced_page(csrf, flash))
        if url.path == "/connect/wait":
            to, body = wait_page(q.get("flow", [""])[0])
            return self.redirect(to) if to else self.send(200, body)
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
        if url.path == "/calendar":
            with state_lock:
                problem = op_errors.pop() if op_errors else None
            if q.get("undo") and not problem:      # the undo bar says it already
                flash = None
            return self.send(200, calendar_page(csrf, q, problem or flash))
        if url.path == "/event":
            return self.send(200, event_page(csrf, q, flash))
        if url.path == "/event/new":
            return self.send(200, new_event_page(csrf, q, flash))
        if url.path == "/search":
            return self.send(200, search_page(csrf, q))
        if url.path == "/activity":
            return self.send(200, activity_page(csrf, flash))
        if url.path == "/pair":
            src, dst = q.get("src", [""])[0], q.get("dst", [""])[0]
            names = {c["name"] for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
            if src in names and dst in names and src != dst:
                return self.send(200, pair_page(csrf, src, dst, flash))
        self.send(404, page("Not found", "<h1>Page not found</h1>", True))

    def do_POST(self):
        tidy_flows()
        here.host = self.headers.get("Host")
        url = urllib.parse.urlparse(self.path).path
        if not host_allowed(self.headers.get("Host")) or not self.same_origin():
            return self.send(400, "Blocked: this request came from another site.", "text/plain")
        if url.startswith("/hook/"):
            return self.hook(url)
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
        if url == "/forgot":
            return self.forgot(cfg, get)
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
            return self.redirect("/?m=signedout#add")
        if url == "/calendar/add":
            name, account, cal_id = get("name").strip(), get("account"), get("calendar_id")
            if not engine.NAME_OK.fullmatch(name) or account not in engine._accounts():
                return self.send(200, account_page(account, csrf, "Use letters, numbers, - and _ for the name.") if account in engine._accounts() else connect_page(csrf))
            read_only = engine._accounts()[account]["provider"] == "ics"
            if not read_only:
                read_only = not next((c["writable"] for c in engine.account_calendars(account) if c["id"] == cal_id), False)
            if name in [c["name"] for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]]:
                return self.send(200, account_page(account, csrf, f"{name} is already in use. Pick another name."))
            engine.update_config(lambda c: (c["calendars"].append({"name": name, "label": name, "account": account, "calendar_id": cal_id,
                                                                   "busy_only": bool(get("busy_only")), "read_only": read_only, "cannot_write": read_only, "skip_sources": []}),
                                            c.update(kept_sources=[k for k in c.get("kept_sources", []) if k != name])))
            return self.redirect("/?m=added")
        if url == "/calendar/save":
            name = get("name")

            def save(c):
                others = {x["name"] for x in c["calendars"]}
                for x in c["calendars"]:
                    if x["name"] == name:
                        x.update(busy_only=bool(get("busy_only")), read_only=bool(get("read_only")) or x.get("cannot_write", False),
                                 skip_sources=[n for n in form.get("skip", []) if n in others and n != name])
                        if "colour" in form:
                            x["colour"] = get("colour") if get("colour") in engine.PALETTE else ""
            engine.update_config(save)
            return self.redirect("/?m=saved")
        if url == "/calendar/remove":
            name = get("name")
            if not get("confirm"):
                return self.send(200, remove_calendar_page(csrf, name))
            keep = get("copies") == "keep"

            def drop(c):
                c["calendars"] = [x for x in c["calendars"] if x["name"] != name]
                kept = [k for k in c.get("kept_sources", []) if k != name]
                c["kept_sources"] = kept + ([name] if keep else [])
                c["rules"] = {k: v for k, v in c.get("rules", {}).items() if name not in k.split(">")}
            engine.update_config(drop)
            return self.redirect("/?m=" + ("removedkept" if keep else "removed"))
        if url == "/purge":
            cals = [c for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"] if c["name"] == get("name")]
            if not get("confirm"):
                count = engine.purge(cals, {c["name"]: engine.make_adapter(c) for c in cals}, dry_run=True, log=lambda *_: None)["deleted"]
                return self.send(200, page("Delete copies", f'''<h1>Delete {copies(count)} from {esc(get("name"))}?</h1><section><p>This removes only events CalSync made. If syncing is still on, the next run makes them again, so stop syncing this calendar too if you're done with it.</p>
<form method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(get("name"))}"><input type="hidden" name="confirm" value="1"><div class="actions"><button class="danger">Delete {copies(count)}</button><a class="button plain" href="/">Keep them</a></div></form></section>''', True, at="/"))
            s = engine.purge(cals, {c["name"]: engine.make_adapter(c) for c in cals}, log=lambda *_: None)
            return self.send(200, page("Copies deleted", f'<h1>Deleted {copies(s["deleted"])}</h1><section>{esc(s["errors"])} problems.</section><a class="button plain" href="/">Back</a>', True, at="/"))
        if url == "/preview":
            lines, decisions = [], []
            s = engine.run_once(dry_run=True, log=lines.append, decisions=decisions)
            if s is None:
                return self.redirect("/?m=busy")
            with state_lock:
                last_preview[self.cookie()] = dict(s, lines=lines, decisions=decisions)
            return self.redirect("/")
        if url == "/resync":
            pair = get("pair")
            names = {c["name"] for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
            if ">" not in pair or not set(pair.split(">", 1)) <= names:
                return self.redirect("/")
            return self.redirect("/?m=" + ("resyncing" if run_in_background(pair=pair, refresh=True, trigger="resync") else "busy"))
        if url == "/pair/save":
            src, dst = get("src"), get("dst")
            names = {c["name"] for c in engine.load_json(engine.CONFIG, {"calendars": []})["calendars"]}
            if src not in names or dst not in names or src == dst:
                return self.redirect("/")
            rule = rule_from_form(form)

            def keep(c):
                c.setdefault("rules", {})[f"{src}>{dst}"] = rule
                for x in c["calendars"]:        # the older setting would say the opposite of this rule, so keep them in step
                    if x["name"] == dst:
                        x["skip_sources"] = [n for n in x.get("skip_sources", []) if n != src] + ([] if rule["enabled"] else [src])
            engine.update_config(keep)
            return self.redirect("/?m=rulesaved")
        if url == "/start":
            engine.update_config(lambda c: c.update(enabled=True))
            run_in_background(trigger="start")
            return self.redirect("/?m=started")
        if url == "/pause":
            engine.update_config(lambda c: c.update(enabled=False))
            return self.redirect("/?m=paused")
        if url == "/run":
            return self.redirect("/?m=" + ("running" if run_in_background(force=bool(get("force")), trigger="manual") else "busy"))
        if url == "/quick":
            text, cal, back = get("text").strip(), get("calendar"), back_to(get("back"))
            q = dict(urllib.parse.parse_qs(urllib.parse.urlparse(back).query), text=[text], cal=[cal])
            ev, why = engine.quick_plan(text)
            if not ev:
                return self.send(200, calendar_page(csrf, q, f"Couldn't read that: {why}."))
            target = next((c for c in engine.writable_calendars() if c["name"] == cal), None)
            if not target:
                return self.send(200, calendar_page(csrf, q, "Pick a calendar to add it to."))
            if ev["guests"] and not isinstance(engine.make_adapter(target), engine.GoogleCalendar):
                return self.send(200, calendar_page(csrf, q, "Invites go out from a Google calendar. Pick one, or leave the addresses out."))
            overlaps, unread = engine.busy_overlaps(ev)
            plan = {"event": ev, "calendar": target.get("label") or target["name"], "overlaps": overlaps, "unread": unread}
            return self.send(200, calendar_page(csrf, q, None, quick_preview(csrf, plan, text, cal, back)))
        if url == "/quick/add":
            back = back_to(get("back"))
            r = engine.quick_add(get("text"), get("calendar"), confirm_overlap=bool(get("book_anyway")), confirm_guests=bool(get("send_invites")))
            q = dict(urllib.parse.parse_qs(urllib.parse.urlparse(back).query), text=[get("text")], cal=[get("calendar")])
            if r["status"] == "invalid":
                return self.send(200, calendar_page(csrf, q, f"Not added: {r['why']}."))
            if r["status"] == "confirm":        # something changed since the preview: show it again
                return self.send(200, calendar_page(csrf, q, None, quick_preview(csrf, r, get("text"), get("calendar"), back)))
            with state_lock:
                CAL_CACHE.clear()
            return self.redirect(with_flash(back, "quick" if r.get("synced") else "quicklater"))
        if url in ("/skip", "/unskip"):
            back = back_to(get("back"))
            if url == "/skip":
                engine.skip_occurrence(get("key"), get("title"), get("start"))
            else:
                engine.unskip_occurrence(get("key"))
            with state_lock:
                CAL_CACHE.clear()
            synced = engine.load_json(engine.CONFIG, {}).get("enabled") and run_in_background()
            return self.redirect(with_flash(back, ("skipped" if url == "/skip" else "unskipped") + ("" if synced else "later")))
        if url == "/settings/save":
            minutes, zone = get("interval"), get("timezone").strip()
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(zone)
            except Exception:
                return self.send(200, settings_page(csrf, "That time zone isn't known. Use a name like America/New_York."))
            if not minutes.isdigit() or int(minutes) not in INTERVALS:
                return self.send(200, settings_page(csrf, "Pick one of the listed intervals."))
            add_to = get("add_to")
            if add_to and add_to not in {c["name"] for c in engine.writable_calendars()}:
                return self.send(200, settings_page(csrf, "Pick one of the listed calendars for quick add."))
            tz2 = get("tz2").strip()
            if tz2 and not _zone(tz2):
                return self.send(200, settings_page(csrf, "That second time zone isn't known. Use a name like Europe/London."))
            try:
                window = {"past_days": min(max(int(get("past_days") or engine.PAST_DAYS), 0), 365),
                          "future_days": min(max(int(get("future_days") or engine.FUTURE_DAYS), 1), 730)}
            except ValueError:
                return self.send(200, settings_page(csrf, "Use whole numbers of days."))

            def save_settings(c):
                c.update(interval_minutes=int(minutes), timezone=zone, window=window, fast_check=bool(get("fast_check")), **({"add_to": add_to} if add_to else {}))
                c["view"] = dict(c.get("view") or {}, tz2=tz2)
            engine.update_config(save_settings)
            return self.redirect("/settings?m=saved")
        if url in ("/event/create", "/event/save", "/event/delete", "/event/move", "/event/rsvp", "/undo", "/calendar/prefs"):
            return self.calendar_action(url, get, form, csrf)
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
        if url == "/settings/recovery":
            ip = self.client_address[0]
            stamp = take_try("login", ip)
            if stamp is None:
                return self.send(429, settings_page(csrf, "Too many tries. Wait a minute and try again."))
            if not hmac.compare_digest(hash_password(get("old"), cfg["salt"]), cfg["hash"]):
                return self.send(200, settings_page(csrf, "The current password isn't right."))
            give_back("login", ip, stamp)
            return self.send(200, recovery_page(new_recovery_code(), "Your new recovery code", "The old code no longer works."))
        self.send(404, page("Not found", "<h1>Page not found</h1>", True))

    def calendar_action(self, url, get, form, csrf):
        """New, changed, moved and deleted events. A change or delete waits UNDO_SECONDS, so Undo just drops it."""
        back = back_to(get("back"))
        if url == "/calendar/prefs":
            key = get("pref")
            if key in ("hide_copies", "hide_weekends", "week_numbers"):
                engine.update_config(lambda c: c.update(view=dict(c.get("view") or {}, **{key: not (c.get("view") or {}).get(key)})))
            return self.redirect(back)
        if url == "/undo":
            with state_lock:
                op = pending_ops.get(get("token"))
                if op and op["undo_until"] >= time.time() and op["kind"] in ("update", "delete") and not op.get("done"):
                    pending_ops.pop(get("token"))
                    return self.redirect(with_flash(back, "undone"))
            if op and op["kind"] == "created" and op["undo_until"] >= time.time():
                with state_lock:
                    pending_ops.pop(get("token"), None)
                engine.event_delete(op["cal"], op["uid"], "all", False)
                with state_lock:
                    CAL_CACHE.clear()
                return self.redirect(with_flash(back, "undone"))
            return self.redirect(with_flash(back, "toolate"))
        if url == "/event/create":
            raw = event_from_form(get, form)
            try:
                made = engine.event_create(get("cal"), raw, notify=bool(get("notify")))
            except ValueError as e:
                return self.send(200, new_event_page(csrf, {"d": [get("date")], "cal": [get("cal")]}, f"Not added: {e}."))
            token = secrets.token_urlsafe(12)
            with state_lock:
                CAL_CACHE.clear()
                pending_ops[token] = {"kind": "created", "cal": get("cal"), "uid": made, "said": "Added.", "back": back,
                                      "due": 0, "undo_until": time.time() + UNDO_SECONDS, "done": True, "shown": {}}
            if engine.load_json(engine.CONFIG, {}).get("enabled"):
                run_in_background(trigger="manual")
            return self.redirect(with_flash(back, "added_ev") + "&undo=" + token)
        cal, uid, href, scope = get("cal"), get("uid"), get("href"), get("scope") or "this"
        notify = bool(get("notify"))
        if url == "/event/rsvp":
            engine.event_respond(cal, uid, get("answer"))
            with state_lock:
                CAL_CACHE.clear()
            return self.redirect(with_flash(back, "answered"))
        if url == "/event/delete":
            token = queue_op("delete", cal, uid, {}, scope, notify, href, "Deleted.", back)
            return self.redirect(with_flash(back, "pending") + "&undo=" + token)
        if url == "/event/save":
            raw = event_from_form(get, form)
            if not get("repeat"):
                raw.pop("repeat", None)
            try:
                checked = engine.clean_event(raw, partial=True)
            except ValueError as e:
                return self.redirect(with_flash(back, "bad"))
            shown = {"title": checked.get("title"), "all_day": checked.get("all_day"),
                     "start": engine._ics_time_text(checked["start"], checked["all_day"]), "end": engine._ics_time_text(checked["end"], checked["all_day"])}
            token = queue_op("update", cal, uid, raw, scope, notify, href, "Saved.", back, shown)
            return self.redirect(with_flash(back, "pending") + "&undo=" + token)
        # /event/move, from dragging in the day and week views: only the time changes
        D = engine.dt
        tz = engine.local_tz()
        try:
            s = D.datetime.fromisoformat(f"{get('date')}T{get('start')}").replace(tzinfo=tz)
            e = D.datetime.fromisoformat(f"{get('date')}T{get('end')}").replace(tzinfo=tz) if get("end") != "24:00" else D.datetime.fromisoformat(get("date")).replace(tzinfo=tz) + D.timedelta(days=1)
        except ValueError:
            return self.send(400, json.dumps({"error": "bad time"}), "application/json")
        if e <= s:
            e = s + D.timedelta(minutes=15)
        shown = {"start": engine._ics_time_text(s, False), "end": engine._ics_time_text(e, False), "all_day": False}
        token = queue_op("update", cal, uid, {"start": s, "end": e, "all_day": False}, "this", notify, href, "Moved.", back, shown)
        return self.send(200, json.dumps({"go": with_flash(back, "pending") + "&undo=" + token}), "application/json")

    def hook(self, url):
        """Google and Microsoft calling to say a calendar changed (only with a public https address). Each proves
        itself with the secret calsync gave it; the body is never trusted for more than "something changed"."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if url == "/hook/microsoft" and "validationToken" in q:     # Graph checks the address before it subscribes
            return self.send(200, q["validationToken"][0][:512], "text/plain")
        secret = (engine.load_json(push_file(), {}) or {}).get("secret")
        if not secret:
            return self.send(404, "", "text/plain")
        if url == "/hook/google":
            ok = hmac.compare_digest((self.headers.get("X-Goog-Channel-Token") or "").encode(), secret.encode())
        elif url == "/hook/microsoft":
            form = self.read_body_json()
            ok = bool(form) and all(hmac.compare_digest(str(v.get("clientState", "")).encode(), secret.encode()) for v in form.get("value", [{}]))
        else:
            ok = False
        if not ok:
            return self.send(403, "", "text/plain")
        pushed.set()
        return self.send(202 if url == "/hook/microsoft" else 200, "", "text/plain")

    def read_body_json(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if 0 <= size <= MAX_BODY:
                return json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, OSError):
            pass
        return None

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
        return page("Welcome", f'''<h1>Welcome to CalSync</h1><section><p>Choose a password for this page. Your sign-ins stay on this machine, so this password is what guards them.</p>
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
            code = new_recovery_code()
        return self.send(200, recovery_page(code, "Your password is set"), headers=[self.session_header(make_session(web_config()), SESSION_DAYS * 86400)])

    def forgot_page(self, flash=None):
        """Forgot the password: both ways back only work on the machine calsync runs on, never from somewhere else."""
        here = not self.needs_code()
        form = ('''<h2>With your recovery code</h2><section><p>You were given a recovery code when you chose your password. It works only on this computer.</p>
<form method="post" action="/forgot" autocomplete="off"><label for="rc">Recovery code</label><input type="text" id="rc" name="code" required placeholder="ABCD-EFGH-JKLM-NPQR" autocapitalize="characters" spellcheck="false">
<label for="np">New password, at least 10 characters</label><input type="password" id="np" name="password" required minlength="10" autocomplete="new-password"><div class="actions"><button>Reset password</button></div></form></section>'''
                if here else '<h2>With your recovery code</h2><section class="note">For safety, a recovery code only works on the computer CalSync runs on. Open this page there, or use the command below.</section>')
        return page("Forgot password", f'''<h1>Forgot your password?</h1><section><p>CalSync runs on your own computer, so there is no account and no email reset. You can still get back in from the computer it runs on.</p></section>
<h2>From the computer it runs on</h2><section><p>Open a terminal there and run:</p><pre>calsync reset-password</pre><p class="soft">With Docker: <code>docker compose exec calsync calsync reset-password</code>. You choose a new password, every browser is signed out, and you get a new recovery code.</p></section>
{form}<div class="actions"><a class="button plain" href="/">Back to sign in</a></div>''', flash=flash)

    def forgot(self, cfg, get):
        if self.needs_code():       # never from another machine, a proxy or the Docker port
            return self.send(403, self.forgot_page("A recovery code only works on the computer CalSync runs on."))
        ip = self.client_address[0]
        stamp = take_try("recover", ip)
        if stamp is None:
            return self.send(429, self.forgot_page("Too many tries. Wait a minute and try again."))
        if not recovery_ok(cfg, get("code")):
            return self.send(200, self.forgot_page("That recovery code isn't right."))
        give_back("recover", ip, stamp)
        if len(get("password")) < 10:
            return self.send(200, self.forgot_page("Use at least 10 characters."))
        set_password(get("password"))
        code = new_recovery_code()      # a used code is spent
        return self.send(200, recovery_page(code, "Your password is reset", "Every other browser is signed out. Your old recovery code no longer works, so keep this new one."),
                         headers=[self.session_header(make_session(web_config()), SESSION_DAYS * 86400)])

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
<li>Google warns that the app isn't verified, because it's yours. Click Advanced, Go to CalSync, then Allow.</li>
<li>If the browser ends on a page that won't load, copy that page's whole address and paste it here.</li></ol>
<form method="post" action="/connect/google/paste" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}"><label for="addr">Address from the browser</label><input type="text" id="addr" name="address" required><div class="actions"><button class="plain">Finish sign-in</button></div></form></section>''', True, at="/"))

    def google_callback(self, q):
        state = q.get("state", [""])[0]
        with state_lock:
            flow = pending_google.pop(state, None)
        if not flow:
            return self.send(400, page("Connect Google", '<h1>This sign-in link has expired</h1><section>Start again from Add a calendar.</section><a class="button plain" href="/#add">Add a calendar</a>'))
        if "code" not in q:
            return self.send(200, page("Connect Google", '<h1>Google didn\'t finish the sign-in</h1><section>Nothing changed.</section><a class="button plain" href="/#add">Back</a>'))
        problem = None
        try:
            problem = engine.google_finish(flow["label"], q["code"][0], flow["redirect"])
        except Exception as e:
            problem = short_error(e)
        if problem:
            return self.send(200, page("Connect Google", f'<h1>That didn\'t work</h1><section class="bad">{esc(problem)}</section><a class="button plain" href="{esc(flow["origin"])}/#add">Back</a>'))
        self.redirect(f"{flow['origin']}/account?label={urllib.parse.quote(flow['label'])}")

    def oauth_callback(self, provider, q):
        """Google or Microsoft sending the browser back after one-click sign-in. The one-use state proves it's ours,
        and the PKCE verifier that only this server holds is what turns the code into a sign-in."""
        state = q.get("state", [""])[0]
        with state_lock:
            flow = pending_oauth.get(state)
            if flow and (flow["provider"] != provider or flow["status"] != "waiting"):
                flow = None
            if flow:
                flow["status"] = "working"
        if not flow:
            return self.send(400, page("Sign in", '<h1>This sign-in link has expired</h1><section>Start again from Add a calendar in CalSync.</section>'))
        name = "Google" if provider == "google" else "Microsoft"
        params = {k: v[0] for k, v in q.items()}
        try:
            if "code" not in params:
                raise RuntimeError(engine.oauth_refused(provider, params))
            label = engine.oauth_finish(provider, params["code"], flow["verifier"], flow["redirect"])
            flow.update(status="done", label=label)
        except engine.SignInBlocked:
            flow["status"] = "blocked"
            return self.send(200, page("Sign in", f'<h1>{name} didn\'t let CalSync in</h1>{blocked_section()}'))
        except Exception as e:
            flow.update(status="failed", problem=short_error(e))
            return self.send(200, page("Sign in", f'<h1>That didn\'t work</h1><section class="bad">{esc(short_error(e))}</section>'))
        if session_ok(web_config(), self.cookie()):
            return self.redirect("/account?label=" + urllib.parse.quote(label))
        return self.send(200, page("Signed in", f'''<h1>Signed in to {name}</h1><section><p>You can close this tab and go back to CalSync,
where you choose which calendars to sync.</p></section>'''))

    def google_paste(self, address):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(address.strip()).query)
        return self.google_callback(q)

    def microsoft_wait(self, flow_id):
        with state_lock:
            flow = pending_ms.get(flow_id)
        if not flow:
            return self.send(200, page("Connect Microsoft", '<h1>This sign-in has expired</h1><a class="button plain" href="/connect/microsoft">Start again</a>', True, at="/"))
        if flow["status"] == "done":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.redirect("/account?label=" + urllib.parse.quote(flow["label"]))
        if flow["status"] == "failed":
            with state_lock:
                pending_ms.pop(flow_id, None)
            return self.send(200, page("Connect Microsoft", f'<h1>That didn\'t work</h1><section class="bad">{esc(flow.get("problem", ""))}</section><a class="button plain" href="/connect/microsoft">Try again</a>', True, at="/"))
        return self.send(200, page("Connect Microsoft", f'''<h1>Sign in to Microsoft</h1><section><ol><li><a class="button" href="{esc(flow["link"])}" target="_blank" rel="noreferrer noopener">Open Microsoft sign-in</a></li><li>Enter this code:<p class="code">{esc(flow["user_code"])}</p></li><li>Sign in and accept. This page moves on by itself.</li></ol></section>''', True, refresh=5, at="/"))


def back_to(path):
    """Only back to a Calendar page: never somewhere else a form could be pointed at."""
    return path if re.fullmatch(r"/calendar(\?[A-Za-z0-9=&%._+-]*)?", path or "") else "/calendar"


def with_flash(path, key):
    return path + ("&" if "?" in path else "?") + "m=" + key


FLASHES = {"saved": "Saved.", "added": "Added. Preview to see what will change.", "removed": "Stopped syncing that calendar. Its copies on other calendars go away on the next run.",
           "busy": "A sync is already going. Try again in a moment.", "started": "Syncing started.", "paused": "Paused. Nothing changes until you start it again.",
           "running": "Sync started.", "signedout": "Signed out of that account.",
           "quick": "Added and synced. A booking page reading your calendars sees that time as busy now.",
           "quicklater": "Added. It reaches your other calendars at the next sync.",
           "skipped": "Skipped. Its copies are being removed, so the time opens on your other calendars.",
           "skippedlater": "Skipped. Its copies go at the next sync.",
           "unskipped": "Copying it again.", "unskippedlater": "It's copied again at the next sync.",
           "rulesaved": "Saved. It applies from the next sync; Preview shows what changes.", "resyncing": "Resyncing that pair now.",
           "removedkept": "Stopped syncing that calendar. Its copies stay where they are.",
           "added_ev": "Added.", "pending": "Saving in a few seconds.", "undone": "Undone. Nothing was changed.",
           "toolate": "Too late to undo that one.", "answered": "Answer sent.", "bad": "That change couldn't be read."}


def login_page(flash=None):
    return page("Sign in", '<h1>Sign in</h1><section><form method="post" action="/login"><label for="pw">Password</label><input type="password" id="pw" name="password" required autofocus><div class="actions"><button>Sign in</button></div></form></section>'
                '<p class="center"><a href="/forgot">Forgot password?</a></p>', flash=flash)


def recovery_page(code, title, note="If you ever forget your password, this code lets you choose a new one from this computer."):
    """Shown once: only the code's hash is kept."""
    return page("Recovery code", f'''<h1>{esc(title)}</h1><section><p><b>Save this recovery code somewhere safe</b>, like a password manager or on paper. It's shown only now.</p>
<p class="code">{esc(code)}</p><p class="soft">{esc(note)} You can also reset the password with <code>calsync reset-password</code> on this computer.</p>
<div class="actions"><a class="button" href="/">I saved it, continue</a></div></section>''', flash=None)


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


def serve(host=None, port=None, home=None, block=True):
    """Run the page and the background sync. host, port and home override CALSYNC_BIND, CALSYNC_PORT and CALSYNC_HOME,
    so the desktop app or a hosted copy can start it from code. block=False returns (server, stop) with both running
    in threads; call server.shutdown() and stop.set() to end them."""
    global BIND, PORT
    if home:
        engine.set_home(home)
    BIND, PORT = host or BIND, int(port if port is not None else PORT)
    engine.ensure_home()
    server = Server((BIND, PORT), Handler)
    PORT = server.server_address[1]
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(stop,), daemon=True).start()
    if not block:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, stop

    def stop_now(*_):       # as a container's first process, nothing stops it on SIGTERM unless it asks to
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_now)
    print(f"CalSync is on port {PORT} (open http://localhost:{PORT})", flush=True)
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
