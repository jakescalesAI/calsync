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
EVERYONE_PER_MINUTE = {"login": 30, "setup": 10, "recover": 10}   # and from all addresses together, so many addresses don't help
RECOVERY_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L: a code someone copies by hand
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
    new = getpass.getpass("New password for the calsync page (at least 10 characters, hidden): ")
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
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="calsync"><defs><linearGradient id="t" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="512"><stop offset="0" stop-color="#2c3648"/><stop offset="1" stop-color="#0b0f16"/></linearGradient><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="0" y1="96" x2="0" y2="420"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#d9e1ec"/></linearGradient></defs><rect width="512" height="512" rx="114" fill="url(#t)"/><rect x="112" y="140" width="288" height="276" rx="56" fill="url(#g)"/><rect x="154" y="88" width="60" height="104" rx="30" fill="url(#t)"/><rect x="166" y="100" width="36" height="80" rx="18" fill="url(#g)"/><rect x="298" y="88" width="60" height="104" rx="30" fill="url(#t)"/><rect x="310" y="100" width="36" height="80" rx="18" fill="url(#g)"/><g fill="none" stroke="#0b0f16" stroke-width="20" stroke-linecap="round" stroke-linejoin="round"><path d="M154 305V270A46 46 0 0 1 200 224H312A46 46 0 0 1 358 270V311M336 289L358 311L380 289"/><path transform="rotate(180 256 309)" d="M154 305V270A46 46 0 0 1 200 224H312A46 46 0 0 1 358 270V311M336 289L358 311L380 289"/></g><path transform="translate(256 309) scale(4.4) translate(-31.8 -38.6)" d="M25.78 50.67Q23.3 50.67 21.39 49.81Q19.47 48.96 18.37 47.46Q17.26 45.95 17.2 43.98L21.44 43.98Q21.5 44.94 22.08 45.65Q22.66 46.36 23.62 46.75Q24.58 47.13 25.78 47.13Q27.04 47.13 28.01 46.69Q28.98 46.25 29.54 45.45Q30.1 44.66 30.1 43.62Q30.1 42.54 29.53 41.72Q28.96 40.9 27.91 40.44Q26.85 39.97 25.35 39.97L23.27 39.97L23.27 36.72L25.35 36.72Q26.58 36.72 27.51 36.29Q28.44 35.86 28.96 35.08Q29.49 34.31 29.49 33.28Q29.49 32.29 29.04 31.55Q28.59 30.81 27.76 30.39Q26.93 29.97 25.83 29.97Q24.74 29.97 23.82 30.36Q22.91 30.75 22.34 31.46Q21.77 32.17 21.74 33.17L17.69 33.17Q17.72 31.2 18.82 29.71Q19.91 28.22 21.75 27.38Q23.59 26.53 25.86 26.53Q28.21 26.53 29.94 27.42Q31.68 28.3 32.63 29.78Q33.59 31.25 33.59 33.03Q33.59 35.02 32.42 36.36Q31.25 37.7 29.34 38.13L29.34 38.32Q30.98 38.54 32.12 39.29Q33.25 40.04 33.85 41.2Q34.44 42.37 34.44 43.84Q34.44 45.81 33.32 47.35Q32.2 48.9 30.25 49.78Q28.3 50.67 25.78 50.67ZM46.4 26.85L46.4 50.35L42.22 50.35L42.22 30.6L42.06 30.6L36.58 34.55L36.58 30.54L41.73 26.85Z" fill="#0b0f16" stroke="#0b0f16" stroke-width="0.6" stroke-linejoin="round"/></svg>'
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
@media (min-width:900px){.cols{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:0 32px;align-items:start}.cols>div>h2:first-child{margin:0 0 20px;padding:0;color:var(--text);font:700 34px/1.1 var(--display);letter-spacing:-.03em;text-transform:none}}
@media (max-width:560px){header{position:static}.bar{padding:10px 16px}nav{flex:1 1 100%}nav a{flex:1;padding:6px 4px;text-align:center}main{padding:24px 16px 72px}h1{font-size:30px}
section{padding:16px}.list,.cal{padding:0}.row,summary{padding:12px 16px}.row+.row::before{left:68px}.opts{margin-left:16px;padding:16px 16px 16px 0}.chips{order:1;flex-basis:100%;padding-left:48px}.row.long>div{flex-basis:100%}.tally li{padding:10px;font-size:14px}.out .bar{padding-top:40px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

NAV = (("/", "Sync"), ("/calendar", "Calendar"), ("/settings", "Settings"))


def page(title, body, signed_in=False, flash=None, refresh=None, at="", wide=False):
    """at is the header link this page belongs under; wide gives the two-column sync page room."""
    links = "".join('<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if href == at else "", name) for href, name in NAV)
    nav = f'<nav aria-label="Main">{links}</nav>' if signed_in else ""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    note = f'<p class="flash" role="status">{esc(flash)}</p>' if flash else ""
    size = 32 if signed_in else 64
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta name="theme-color" content="#ffffff">{meta}'
            f'<title>{esc(title)} · calsync</title><link rel="stylesheet" href="/app.css"><link rel="icon" href="{MARK}">'
            f'<body class="{"in" if signed_in else "out"}"><header><div class="bar"><a class="brand" href="/"><img src="{MARK}" alt="" width="{size}" height="{size}">calsync</a>{nav}</div></header>'
            f'<main{" class=wide" if wide else ""}>{note}{body}</main></html>')


def tile(name):
    """The square with a calendar's or an account's first letter, on the tile of calsync's icon."""
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
{skip}<div class="actions"><button>Save</button></div></form>
<div class="actions"><form class="inline" method="post" action="/calendar/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="plain">Stop syncing this calendar</button></form>
<form class="inline" method="post" action="/purge"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="name" value="{esc(c["name"])}"><button class="danger">Delete the copies calsync made here</button></form></div></div></details></section>''')
    if len(cals) < 2:
        out.append('<section class="note">Add at least two calendars and calsync can start.</section>')
    out.append(f'<div class="actions"><a class="button plain" href="#add">Add a calendar</a><a class="button plain" href="/calendar">See them on a calendar</a></div>'
               f'<h2 id="add" class="big">Add a calendar</h2>{connect_body(csrf)}</div><div><h2>Sync</h2>')
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
    return f'''{existing}<h2>Connect an account</h2>
<section class="list"><div class="row"><div><b>Google Calendar</b></div><a class="button" href="/connect/google">Connect</a></div>
<div class="row"><div><b>Outlook or Microsoft 365</b></div><a class="button" href="/connect/microsoft">Connect</a></div>
{caldav}</section>
<section class="list"><div class="row long"><div><b>Published .ics link</b> <span class="soft">Read-only: its events are copied out, nothing is written back. For Proton Calendar, an Exchange server, or an organization that blocks outside apps.</span></div><a class="button" href="/connect/ics">Add link</a></div>
<div class="row long"><div><b>Calendly, Cal.com or another booking app</b> <span class="soft">Nothing to add here. Connect the booking app to any calendar above. calsync keeps that calendar showing your busy times from all the others.</span></div><a class="button plain" href="https://docs.getcalsync.app/booking-apps/">How it works</a></div></section>'''


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
    return page("Connect Google", f'<h1>Connect Google</h1><section><p>Google needs a sign-in app of your own. It\'s free and takes about 10 minutes. You only do it once, for all your Google accounts.</p>{GOOGLE_STEPS}{client_form(csrf)}</section>', True, flash, at="/")


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
<div class="actions"><button>Continue</button></div></form></section>''', True, flash, at="/")


def ics_page(csrf, flash=None):
    return page("Add an .ics link", f'''<h1>Add a published .ics link</h1><section><p>calsync saves this link and never shows it again, since anyone who has it can read that calendar.</p>
<form method="post" action="/connect/ics/save" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="label">A short name</label><input type="text" id="label" name="label" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="classes">
<label for="url">Calendar link</label><input type="password" id="url" name="url" required>
<div class="actions"><button>Check and save</button></div></form></section>''', True, flash, at="/")


def caldav_page(kind, csrf, flash=None):
    name, server, note = engine.CALDAV_SERVERS[kind]
    address = "" if server else '<label for="server">Server address</label><input type="text" id="server" name="server" required placeholder="https://cloud.example.com">'
    return page(f"Connect {name}", f'''<h1>Connect {name}</h1><section><p>{note}</p><p>calsync saves this password on this machine and never shows it again.</p>
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
        ro = "" if c["writable"] else '<p class="soft">calsync can read this one but not write to it.</p>'
        rows.append(f'''<section><p class="head">{tile(c["name"])}<b>{esc(c["name"])}</b></p>{ro}<form method="post" action="/calendar/add"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="account" value="{esc(label)}"><input type="hidden" name="calendar_id" value="{esc(c["id"])}">
<label for="n{i}">Name in calsync</label><input type="text" id="n{i}" name="name" value="{esc(slug)}" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9_-]*">
{'<label class="check"><input type="checkbox" name="busy_only"> Copies here only say Busy</label>' if c["writable"] else ""}
<div class="actions"><button>Sync this calendar</button></div></form></section>''')
    listed = "".join(rows) or '<section class="note">This account has no calendars.</section>'
    return page("Choose calendars", f'<h1>{esc(label)}: choose calendars</h1>{listed}<div class="actions"><a class="button plain" href="/">Back to sync</a></div>', True, flash, at="/")


def settings_page(csrf, flash=None):
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    current = cfg.get("interval_minutes", 5)
    opts = "".join(f'<option value="{m}"{" selected" if m == current else ""}>Every {m} minutes</option>' for m in INTERVALS)
    made = ((web_config() or {}).get("recovery") or {}).get("made")
    recovery_state = f"You have a recovery code, made {esc(made)}." if made else "<b>You don't have a recovery code yet.</b> Make one now, so a forgotten password isn't a problem."
    return page("Settings", f'''<h1>Settings</h1><section><form method="post" action="/settings/save"><input type="hidden" name="csrf" value="{csrf}">
<label for="iv">How often to sync</label><select id="iv" name="interval">{opts}</select>
<label for="tz">Your time zone</label><input type="text" id="tz" name="timezone" value="{esc(cfg.get("timezone") or engine.tz_name())}" required>
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


def calendar_page(csrf, q, flash=None):
    D = engine.dt
    cfg = engine.load_json(engine.CONFIG, {"calendars": []})
    cals = cfg["calendars"]
    if not cals:
        return page("Calendar", '<h1>Calendar</h1><section class="empty"><p>Add a calendar first, and its events show up here.</p><a class="button" href="/#add">Add a calendar</a></section>', True, flash, at="/calendar")
    tz = engine.local_tz()
    today = D.datetime.now(tz).date()
    view = "month" if q.get("view", [""])[0] == "month" else "week"
    try:
        anchor = D.date.fromisoformat(q.get("d", [""])[0])
    except ValueError:
        anchor = today
    names = [c["name"] for c in cals]
    show = q.get("show", [""])[0]
    show = show if show in names else ""
    colour = {n: f"k{i % COLOURS}" for i, n in enumerate(names)}
    label = {c["name"]: c.get("label") or c["name"] for c in cals}

    if view == "week":
        first = anchor - D.timedelta(days=(anchor.weekday() + 1) % 7)      # weeks start on Sunday
        days = [first + D.timedelta(days=i) for i in range(7)]
        back, fwd = first - D.timedelta(days=7), first + D.timedelta(days=7)
        title = f"{first.strftime('%b')} {first.day} to {days[-1].strftime('%b')} {days[-1].day}, {days[-1].year}"
    else:
        start = anchor.replace(day=1)
        first = start - D.timedelta(days=(start.weekday() + 1) % 7)
        nxt = (start + D.timedelta(days=32)).replace(day=1)
        last = nxt - D.timedelta(days=1)
        count = ((last - first).days // 7 + 1) * 7
        days = [first + D.timedelta(days=i) for i in range(count)]
        back, fwd = (start - D.timedelta(days=1)).replace(day=1), nxt
        title = start.strftime("%B %Y")
    lo = D.datetime.combine(days[0], D.time(), tz).astimezone(D.timezone.utc)
    hi = D.datetime.combine(days[-1] + D.timedelta(days=1), D.time(), tz).astimezone(D.timezone.utc)
    tmin, tmax = lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ")

    wanted = [c for c in cals if not show or c["name"] == show]
    fresh = q.get("fresh", [""])[0] == "1"
    results = {}
    threads = [threading.Thread(target=lambda c=c: results.__setitem__(c["name"], calendar_events(c, tmin, tmax, fresh))) for c in wanted]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    per_day = {d: [] for d in days}
    seen, problems = set(), []
    for c in wanted:
        kind, evs = results.get(c["name"], ("err", "took too long to answer"))
        if kind == "err":
            problems.append(f"{label[c['name']]}: {evs}")
            continue
        for ev in evs:
            src = (ev.get("copy_of") or "").split("|")[0]
            if not show and src:            # Everything: only real events; their copies are the same events again
                continue
            ident = ev.get("ical") or ev["uid"]
            if not show and (ident, ev["start"]) in seen:     # one meeting you were invited to on two calendars
                continue
            seen.add((ident, ev["start"]))
            first_day, last_day, when = _local(ev, tz)
            owner = src if src in colour else c["name"]
            what = "Busy" if ev.get("private") and src else (ev.get("title") or "(no title)")
            item = (0 if ev["all_day"] else 1, ev["start"], when, what, colour[owner], bool(src), label.get(owner, owner))
            d = max(first_day, days[0])
            while d <= min(last_day, days[-1]) and d - first_day < D.timedelta(days=62):
                per_day[d].append(item)
                d += D.timedelta(days=1)

    def link(**kw):
        p = {"view": view, "d": anchor.isoformat(), "show": show}
        p.update(kw)
        return "/calendar?" + urllib.parse.urlencode({k: v for k, v in p.items() if v})

    seg = "".join(f'<a href="{esc(link(view=v))}"{" aria-current=page" if v == view else ""}>{v.title()}</a>' for v in ("week", "month"))
    step = (f'<a href="{esc(link(d=back.isoformat()))}" aria-label="Earlier">&lsaquo;</a><a href="{esc(link(d=today.isoformat()))}">Today</a>'
            f'<a href="{esc(link(d=fwd.isoformat()))}" aria-label="Later">&rsaquo;</a>')
    legend = [f'<a href="{esc(link(show=""))}"{" aria-current=page" if not show else ""}>Everything</a>']
    legend += [f'<a class="{colour[n]}" href="{esc(link(show=n))}"{" aria-current=page" if show == n else ""}><span class="dot"></span>{esc(label[n])}</a>' for n in names]
    if show:
        explain = (f"What is on {esc(label[show])} right now. Its own events are solid; striped ones are copies calsync made, "
                   f"in the colour of the calendar they came from.")
    else:
        explain = "Your own events from every calendar, one colour each. Once synced, every calendar calsync writes to holds all of these."

    cells = []
    if view == "month":
        cells += [f'<div class="wd">{w}</div>' for w in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]
    for d in days:
        items = sorted(per_day[d])
        cls = " ".join(x for x in ("today" if d == today else "", "other" if view == "month" and d.month != anchor.month else "") if x)
        head = (f'<p class="dh"><span class="dn">{d.day}</span>' + (f'<span class="wn">{d.strftime("%a")}</span>' if view == "week" else "") + "</p>")
        limit = 3 if view == "month" else 50
        evs = "".join(f'<p class="ev {k}{" copy" if cp else ""}" title="{esc(who)}{" (copy)" if cp else ""}"><span class="t">{esc(when)}</span>{esc(t)}</p>'
                      for _, _, when, t, k, cp, who in items[:limit])
        if len(items) > limit:
            evs += f'<a class="plus" href="{esc(link(view="week", d=d.isoformat()))}">{len(items) - limit} more</a>'
        cells.append(f'<div{" class=" + chr(34) + cls + chr(34) if cls else ""}>{head}{evs}</div>')
    bad = "".join(f"<li>{esc(p)}</li>" for p in problems)
    bad = f'<section class="bad"><p>Some calendars couldn\'t be read just now:</p><ul>{bad}</ul></section>' if bad else ""
    body = f'''<div class="calbar"><h1>Calendar</h1><div class="tools"><nav class="seg" aria-label="View">{seg}</nav><span class="step">{step}</span></div></div>
<p class="range">{esc(title)}</p><div class="legend" role="group" aria-label="Calendars">{"".join(legend)}</div>{bad}
<div class="grid {view}">{"".join(cells)}</div>
<p class="calnote">{explain} Times are in {esc(engine.tz_name())}. Read a moment ago; <a href="{esc(link(fresh="1"))}">refresh</a>.</p>'''
    return page("Calendar", body, True, flash, at="/calendar", wide=True)


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
            return self.send(200, calendar_page(csrf, q, flash))
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
            code = new_recovery_code()
        return self.send(200, recovery_page(code, "Your password is set"), headers=[self.session_header(make_session(web_config()), SESSION_DAYS * 86400)])

    def forgot_page(self, flash=None):
        """Forgot the password: both ways back only work on the machine calsync runs on, never from somewhere else."""
        here = not self.needs_code()
        form = ('''<h2>With your recovery code</h2><section><p>You were given a recovery code when you chose your password. It works only on this computer.</p>
<form method="post" action="/forgot" autocomplete="off"><label for="rc">Recovery code</label><input type="text" id="rc" name="code" required placeholder="ABCD-EFGH-JKLM-NPQR" autocapitalize="characters" spellcheck="false">
<label for="np">New password, at least 10 characters</label><input type="password" id="np" name="password" required minlength="10" autocomplete="new-password"><div class="actions"><button>Reset password</button></div></form></section>'''
                if here else '<h2>With your recovery code</h2><section class="note">For safety, a recovery code only works on the computer calsync runs on. Open this page there, or use the command below.</section>')
        return page("Forgot password", f'''<h1>Forgot your password?</h1><section><p>calsync runs on your own computer, so there is no account and no email reset. You can still get back in from the computer it runs on.</p></section>
<h2>From the computer it runs on</h2><section><p>Open a terminal there and run:</p><pre>calsync reset-password</pre><p class="soft">With Docker: <code>docker compose exec calsync calsync reset-password</code>. You choose a new password, every browser is signed out, and you get a new recovery code.</p></section>
{form}<div class="actions"><a class="button plain" href="/">Back to sign in</a></div>''', flash=flash)

    def forgot(self, cfg, get):
        if self.needs_code():       # never from another machine, a proxy or the Docker port
            return self.send(403, self.forgot_page("A recovery code only works on the computer calsync runs on."))
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
<li>Google warns that the app isn't verified, because it's yours. Click Advanced, Go to calsync, then Allow.</li>
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


FLASHES = {"saved": "Saved.", "added": "Added. Preview to see what will change.", "removed": "Stopped syncing that calendar. Its copies on other calendars go away on the next run.",
           "busy": "A sync is already going. Try again in a moment.", "started": "Syncing started.", "paused": "Paused. Nothing changes until you start it again.",
           "running": "Sync started.", "signedout": "Signed out of that account."}


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
