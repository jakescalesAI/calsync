#!/usr/bin/env python3
"""calsync: keep your calendars (Google, Outlook, iCloud and other CalDAV, .ics feeds) showing the same events.

Every run lists each calendar in a window, then makes every writable calendar
hold one tagged copy of every real event from every other calendar. Copies are
recognised by a private tag, so they are never copied again, and calsync never
edits or deletes an event it did not create. Stateless: the tags are the state.

  calsync web                        web setup page and background sync (http://localhost:8484)
  calsync reset-password             forgot the page password: set a new one, on this computer only
  calsync setup google|microsoft     store your OAuth client (hidden prompts)
  calsync auth google LABEL          sign in a Google account (paste-back flow)
  calsync auth outlook LABEL [TENANT] sign in a Microsoft account (device code)
  calsync auth caldav LABEL [SERVER] iCloud, Fastmail, Yahoo, AOL, Zoho, Nextcloud or any CalDAV address (hidden prompt)
  calsync auth ics LABEL             a published .ics link (read-only source)
  calsync calendars                  list calendars on every signed-in account
  calsync add NAME ACCOUNT CAL_ID [--busy-only] [--read-only] [--free] [--only TEXT] [--skip OTHER_NAME ...]
  calsync remove NAME
  calsync run [--dry-run] [--force]  one sync pass
  calsync purge [NAME] [--dry-run]   delete every copy calsync made (on one calendar, or all)
  calsync status                     configured calendars + last run
  calsync new TEXT... [--check]      add an event ("Dentist tomorrow 3pm 1h") to the main calendar, then sync
  calsync mcp [--http] [--allow-write] [--busy-only]   tools for AI agents (read only unless you ask)
  calsync mcp token [--new]          print the --http token, or replace it
Optional, in config.json: quick_add turns new "- " lines of a Markdown note into events; agenda writes a
Markdown agenda and refreshes daily notes' Calendar sections; export writes one .ics per source calendar.
Python stdlib + python-dateutil (for repeating events in .ics links) + tzdata where the system has none.
Config and tokens live in $CALSYNC_HOME, default ~/.calsync (0700).
"""
import base64, contextlib, datetime as dt, getpass, hashlib, ipaddress, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
import xml.etree.ElementTree as ET
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

def set_home(path):
    """Point calsync at a settings folder. For using the engine as a library (the desktop app, the hosted plan):
    call this before anything else, instead of setting CALSYNC_HOME."""
    global HOME, SECRETS, CONFIG, LASTRUN, ACTIVITY
    HOME = os.path.abspath(os.path.expanduser(path))
    SECRETS = os.path.join(HOME, "secrets")
    CONFIG = os.path.join(HOME, "config.json")
    LASTRUN = os.path.join(HOME, "last-run.json")
    ACTIVITY = os.path.join(HOME, "activity.jsonl")
    return HOME


set_home(os.environ.get("CALSYNC_HOME") or "~/.calsync")
TAG = "calsync"                      # Google private extended property key
TAG_X = "calsync_x"                  # a hash of what else a copy carries (notes, colour, privacy, reminders), so a change there is seen
ICS_TAG = "X-CALSYNC"                # the same tag on a CalDAV event
ICS_TAG_X = "X-CALSYNC-X"
COPY_UID = "calsync-"                # every CalDAV copy's UID starts with this, so a copy is known even if a server drops ICS_TAG
COPY_NOTE = re.compile(r"Synced from \S{1,60} by calsync\. Edit the original there")   # the note every copy carries: a published
#   feed keeps it even where it drops the tag (Google's secret address does), so a copy read back through a link is still known
MS_PROP = "String {6f8f3c3e-7a51-4d0b-9a8e-2c1f5b7d9e44} Name calsync"  # Graph extended property id
MS_PROP_X = "String {6f8f3c3e-7a51-4d0b-9a8e-2c1f5b7d9e44} Name calsync_x"
PAST_DAYS, FUTURE_DAYS = 0, 120  # the default sync window: events that already ended are neither copied nor cleaned up
GOOGLE_REDIRECT = "http://127.0.0.1:8765/"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly"
MS_SCOPES = "offline_access Calendars.ReadWrite"
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")


# ---------- small helpers ----------

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


@contextlib.contextmanager
def file_lock(name, blocking=True):
    """Holds a lock across processes and threads. Yields False when blocking=False and someone else has it."""
    ensure_home()
    with open(os.path.join(HOME, name), "a") as f:
        try:
            _take_lock(f, blocking)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            if not fcntl:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _take_lock(f, blocking):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    f.seek(0)
    while True:
        try:
            return msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if not blocking:
                raise BlockingIOError from None
            time.sleep(.1)


def ensure_home():
    os.makedirs(SECRETS, mode=0o700, exist_ok=True)
    os.chmod(HOME, 0o700)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data, mode=0o600):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def http(method, url, token=None, body=None, form=None, headers=None):
    h = dict(headers or {})
    data = None
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code} {e.read()[:300].decode(errors='replace')}")


def to_utc_z(s):
    """RFC3339 with offset -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- event model ----------
# A normalised event: dict(uid, ical, title, start, end, all_day, busy, location,
#   private, declined, copy_of)  ical = the iCalendar UID shared by every invitee's copy of a meeting.  start/end are 'YYYY-MM-DD' (all-day) or UTC 'Z' strings.
# copy_of is "SOURCE_NAME|SOURCE_UID" for calsync copies, else None.
# Optional, where the provider says: description, conference (a join link), attendees [emails], organizer, me_organizer,
#   rsvp ("yes" | "maybe" | "none" | "no", "" when it's yours), kind ("" | "birthday" | "ooo" | "focus"), colours [names],
#   series (the repeating event it belongs to), href (CalDAV), extra (a copy's TAG_X).

CORE = ("title", "start", "end", "all_day", "busy", "location")
# One palette for calendar colours, copy colours and colour filters. Google's event colour ids are mapped to the nearest.
PALETTE = {"blue": ("#0071e3", "9"), "green": ("#34c759", "2"), "orange": ("#ff9500", "6"), "purple": ("#af52de", "3"),
           "pink": ("#ff2d55", "4"), "teal": ("#30b0c7", "7"), "indigo": ("#5856d6", "1"), "brown": ("#a2845e", "8"),
           "red": ("#e3342f", "11"), "yellow": ("#ffcc00", "5"), "forest": ("#248a3d", "10"), "grey": ("#8e8e93", "8")}
GOOGLE_COLOUR = {"1": "indigo", "2": "green", "3": "purple", "4": "pink", "5": "yellow", "6": "orange", "7": "teal",
                 "8": "grey", "9": "blue", "10": "forest", "11": "red"}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DEFAULT_COPY = {"title": True, "title_text": "Busy", "description": False, "location": True, "conference": False,
                "attendees": False, "reminders": False, "private": False}
DEFAULT_FILTERS = {"free": False, "tentative": False, "unanswered": False, "all_day": False, "birthdays": True, "ooo": False,
                   "colours": [], "colour_mode": "exclude", "keywords": [], "keyword_mode": "exclude",
                   "days": [], "from": "", "until": ""}


def rule_for(src, dst, cfg=None):
    """The rule for copying from calendar src to calendar dst (names or configs). A rule saved in cfg["rules"]["src>dst"]
    wins; anything it leaves out comes from the older per-calendar settings, so a config from before rules keeps doing
    exactly what it did: busy_only on dst hides title and location, skip_sources turns the direction off."""
    cfg = cfg or {}
    by_name = {c["name"]: c for c in cfg.get("calendars", [])}
    s = by_name.get(src, {"name": src}) if isinstance(src, str) else dict({"name": ""}, **src)
    d = by_name.get(dst, {"name": dst}) if isinstance(dst, str) else dict({"name": ""}, **dst)
    busy = bool(d.get("busy_only"))
    rule = {"enabled": s["name"] not in d.get("skip_sources", []), "keep_copies": False, "tag": "", "colour": "",
            "copy": dict(DEFAULT_COPY, title=not busy, location=not busy), "filters": dict(DEFAULT_FILTERS)}
    saved = cfg.get("rules", {}).get(f"{s['name']}>{d['name']}") or {}
    for k in ("enabled", "keep_copies", "tag", "colour"):
        if k in saved:
            rule[k] = saved[k]
    rule["copy"].update({k: v for k, v in (saved.get("copy") or {}).items() if k in DEFAULT_COPY})
    rule["filters"].update({k: v for k, v in (saved.get("filters") or {}).items() if k in DEFAULT_FILTERS})
    return rule


def clean_rule(raw):
    """A rule from a form or an agent, held to known keys and sane values before it's saved."""
    raw = raw or {}
    c, f = raw.get("copy") or {}, raw.get("filters") or {}
    words = lambda v: [w.strip()[:60] for w in (v if isinstance(v, list) else str(v or "").split(",")) if w.strip()][:30]
    hhmm = lambda v: v if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v or "")) else ""
    colour = str(raw.get("colour") or "")
    return {"enabled": bool(raw.get("enabled", True)), "keep_copies": bool(raw.get("keep_copies")),
            "tag": re.sub(r"[\x00-\x1f\x7f]", "", str(raw.get("tag") or ""))[:40],
            "colour": colour if colour in PALETTE or colour == "match" else "",
            "copy": {**{k: bool(c.get(k, DEFAULT_COPY[k])) for k in DEFAULT_COPY if k != "title_text"},
                     "title_text": re.sub(r"[\x00-\x1f\x7f]", "", str(c.get("title_text") or "Busy")).strip()[:60] or "Busy"},
            "filters": {**{k: bool(f.get(k, DEFAULT_FILTERS[k])) for k in ("free", "tentative", "unanswered", "all_day", "birthdays", "ooo")},
                        "colours": [x for x in words(f.get("colours")) if x in PALETTE] if f.get("colours") else [],
                        "colour_mode": "only" if f.get("colour_mode") == "only" else "exclude",
                        "keywords": words(f.get("keywords")), "keyword_mode": "only" if f.get("keyword_mode") == "only" else "exclude",
                        "days": sorted({int(x) for x in (f.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6}),
                        "from": hhmm(f.get("from")), "until": hhmm(f.get("until"))}}


def sync_window(cfg=None, now=None):
    """(tmin, tmax) for a sync: cfg["window"] = {"past_days", "future_days"}, by default 0 back and 120 ahead."""
    w = (cfg or {}).get("window") or {}
    past = min(max(int(w.get("past_days", PAST_DAYS)), 0), 365)
    future = min(max(int(w.get("future_days", FUTURE_DAYS)), 1), 730)
    now = now or now_utc()
    z = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    return z(now - dt.timedelta(days=past)), z(now + dt.timedelta(days=future))


def _local_start(ev):
    if ev["all_day"]:
        return dt.datetime.fromisoformat(ev["start"][:10])
    return dt.datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone(local_tz())


def filtered_out(ev, rule):
    """Why the rule's filters leave ev out, or None. Plain words: the Preview shows them."""
    f = rule["filters"]
    title = (ev.get("title") or "").lower()
    if f["free"] and not ev.get("busy", True):
        return "it's marked free"
    if f["tentative"] and ev.get("rsvp") == "maybe":
        return "you answered Maybe"
    if f["unanswered"] and ev.get("rsvp") == "none":
        return "you haven't answered the invite"
    if f["all_day"] and ev.get("all_day"):
        return "it's an all-day event"
    if f["birthdays"] and ev.get("kind") == "birthday":
        return "it's a birthday"
    if f["ooo"] and ev.get("kind") == "ooo":
        return "it's out of office"
    if f["colours"]:
        has = [c for c in ev.get("colours") or [] if c in f["colours"]]
        if f["colour_mode"] == "only" and not has:
            return "its colour isn't one of " + ", ".join(f["colours"])
        if f["colour_mode"] == "exclude" and has:
            return f"it's {has[0]}"
    if f["keywords"]:
        hit = next((w for w in f["keywords"] if w.lower() in title), None)
        if f["keyword_mode"] == "only" and not hit:
            return "its title has none of: " + ", ".join(f["keywords"])
        if f["keyword_mode"] == "exclude" and hit:
            return f'its title has "{hit}"'
    if f["days"] or f["from"] or f["until"]:
        start = _local_start(ev)
        if f["days"] and start.weekday() not in f["days"]:
            return f"it's on a {DAYS[start.weekday()]}"
        if not ev.get("all_day") and (f["from"] or f["until"]):
            hm = start.strftime("%H:%M")
            if (f["from"] and hm < f["from"]) or (f["until"] and hm >= f["until"]):
                return f"it starts at {hm}, outside {f['from'] or '00:00'} to {f['until'] or '24:00'}"
    return None


def not_a_source(ev, src, skips=()):
    """Why an event is never copied anywhere, or None."""
    if ev.get("copy_of"):
        return "it's a copy CalSync made"
    if ev.get("declined"):
        return "you declined it"
    if _canceled(ev):
        return "it was cancelled"
    if f"{src}|{ev['uid']}" in skips:
        return "you skipped this one"
    return None


def render(ev, src_name, dst_cfg, rule=None, src_label=None):
    """What the copy of ev on dst should look like. The CORE fields are compared with what's there; everything else
    a rule can add (notes, privacy, colour, reminders) is summed up in "extra", so a copy made before rules has none
    and isn't rewritten. A private event always says Busy, whatever the rule."""
    rule = rule or rule_for(src_name, dst_cfg, {"calendars": [dst_cfg]})
    cp = rule["copy"]
    private = bool(ev.get("private"))
    show = cp["title"] and not private
    title = (ev.get("title") or "(no title)") if show else cp["title_text"]
    if rule.get("tag"):
        title = f"{rule['tag']} {title}"
    fields = {"title": title, "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"], "busy": ev.get("busy", True),
              "location": (ev.get("location") or "") if cp["location"] and not private else ""}
    notes = []
    if not private:
        if cp["description"] and (ev.get("description") or "").strip():
            notes.append(ev["description"].strip()[:4000])
        if cp["conference"] and ev.get("conference"):
            notes.append("Join: " + ev["conference"])
        if cp["attendees"] and ev.get("attendees"):
            notes.append("Guests: " + ", ".join(ev["attendees"][:50]))
    colour = rule.get("colour") or ""
    if colour == "match":
        colour = next((c for c in ev.get("colours") or [] if c in PALETTE), "")
    extra = {"notes": "\n\n".join(notes), "private": bool(cp["private"]), "colour": colour, "reminders": bool(cp["reminders"])}
    if any(extra.values()):
        fields.update(extra)
        fields["extra"] = hashlib.sha256(json.dumps(extra, sort_keys=True).encode()).hexdigest()[:16]
    fields["label"] = src_label if show else "another calendar"   # a Busy copy doesn't say which calendar it came from either
    return fields


def copy_note(label, fields=None):
    """The description every copy carries. COPY_NOTE matches its first line, so a copy read back through a link is known."""
    note = f"Synced from {label} by calsync. Edit the original there; changes here are overwritten."
    extra = (fields or {}).get("notes")
    return f"{note}\n\n{extra}" if extra else note


def comparable(ev):
    return {"title": ev.get("title") or "", "start": ev["start"], "end": ev["end"],
            "all_day": ev["all_day"], "busy": ev["busy"], "location": ev.get("location") or ""}


def same_copy(have, fields):
    return comparable(have) == {k: fields[k] for k in CORE} and (have.get("extra") or None) == (fields.get("extra") or None)


# ---------- adapters ----------

class GoogleCalendar:
    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, account, cal_id):
        self.account, self.cal_id = account, cal_id
        self._token = None

    def token(self):
        if not self._token:
            self._token = google_access_token(self.account)
        return self._token

    def _url(self, suffix=""):
        return f"{self.API}/calendars/{urllib.parse.quote(self.cal_id, safe='')}/events{suffix}"

    def list(self, tmin, tmax):
        out, page = [], None
        while True:
            q = {"timeMin": tmin, "timeMax": tmax, "singleEvents": "true", "maxResults": "2500", "showDeleted": "false"}
            if page:
                q["pageToken"] = page
            r = http("GET", self._url("?" + urllib.parse.urlencode(q)), self.token())
            for it in r.get("items", []):
                ev = self._norm(it)
                if ev:
                    out.append(ev)
            page = r.get("nextPageToken")
            if not page:
                return out

    @staticmethod
    def _norm(it):
        if it.get("status") == "cancelled" or it.get("eventType") == "workingLocation":
            return None
        s, e = it.get("start", {}), it.get("end", {})
        all_day = "date" in s
        people = it.get("attendees", [])
        me = next((a for a in people if a.get("self")), None)
        declined = bool(me) and me.get("responseStatus") == "declined"
        mine = not me or bool(me.get("organizer")) or bool(it.get("organizer", {}).get("self"))
        conf = it.get("hangoutLink") or next((p.get("uri", "") for p in it.get("conferenceData", {}).get("entryPoints", []) if p.get("entryPointType") == "video"), "")
        props = it.get("extendedProperties", {}).get("private", {})
        return {
            "uid": it["id"], "ical": it.get("iCalUID"), "title": it.get("summary", ""),
            "start": s["date"] if all_day else to_utc_z(s["dateTime"]),
            "end": e["date"] if all_day else to_utc_z(e["dateTime"]),
            "all_day": all_day, "busy": it.get("transparency") != "transparent",
            "location": it.get("location", ""), "private": it.get("visibility") in ("private", "confidential"),
            "declined": declined,
            "copy_of": props.get(TAG), "extra": props.get(TAG_X),
            "description": it.get("description", ""), "conference": conf,
            "attendees": [a["email"] for a in people if a.get("email") and not a.get("resource") and not a.get("self")],
            "organizer": it.get("organizer", {}).get("email", ""), "me_organizer": mine,
            "rsvp": "" if mine else {"accepted": "yes", "tentative": "maybe", "needsAction": "none", "declined": "no"}.get(me.get("responseStatus"), "none"),
            "kind": {"birthday": "birthday", "outOfOffice": "ooo", "focusTime": "focus"}.get(it.get("eventType"), ""),
            "colours": [GOOGLE_COLOUR[it["colorId"]]] if it.get("colorId") in GOOGLE_COLOUR else [],
            "series": it.get("recurringEventId"), "recurrence": it.get("recurrence"),
        }

    def _body(self, fields, copy_of, src_label):
        when = (lambda d: {"date": d}) if fields["all_day"] else (lambda t: {"dateTime": t, "timeZone": "UTC"})
        body = {
            "summary": fields["title"], "location": fields["location"],
            "description": copy_note(src_label, fields),
            "start": when(fields["start"]), "end": when(fields["end"]),
            "transparency": "opaque" if fields["busy"] else "transparent",
            "reminders": {"useDefault": True} if fields.get("reminders") else {"useDefault": False, "overrides": []},
            "extendedProperties": {"private": {TAG: copy_of, **({TAG_X: fields["extra"]} if fields.get("extra") else {})}},
        }
        if fields.get("private"):
            body["visibility"] = "private"
        if fields.get("colour") in PALETTE:
            body["colorId"] = PALETTE[fields["colour"]][1]
        return body

    def create(self, fields, copy_of, src_label):
        return http("POST", self._url(), self.token(), self._body(fields, copy_of, src_label))["id"]

    def update(self, uid, fields, copy_of, src_label):
        http("PUT", self._url("/" + urllib.parse.quote(uid, safe="")), self.token(), self._body(fields, copy_of, src_label))

    def create_native(self, ev):
        """A real event (from quick add), not a copy: untagged, so the next sync copies it everywhere.
        Guests get Google's normal invite email; the caller has already had the person confirm the list."""
        when = (lambda d: {"date": d}) if ev["all_day"] else (lambda t: {"dateTime": t.isoformat(), "timeZone": tz_name()})
        body = {"summary": ev["title"], "start": when(ev["start"]), "end": when(ev["end"]),
                "description": "Added with calsync quick add.",
                "extendedProperties": {"private": {"calsync_quick": "1"}}}
        if ev.get("guests"):
            body["attendees"] = [{"email": g} for g in ev["guests"]]
        if ev.get("remind_minutes") is not None:
            body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": ev["remind_minutes"]}]}
        return http("POST", self._url("?sendUpdates=all" if ev.get("guests") else ""), self.token(), body)["id"]

    def delete(self, uid):
        try:
            http("DELETE", self._url("/" + urllib.parse.quote(uid, safe="")), self.token())
        except RuntimeError as e:
            if "-> 410" not in str(e) and "-> 404" not in str(e):
                raise

    # real events, from the Calendar tab: untagged, so the next sync copies them out through the rules

    def _one(self, uid, q=""):
        return http("GET", self._url("/" + urllib.parse.quote(uid, safe="") + q), self.token())

    def get(self, uid):
        return self._norm(self._one(uid))

    @staticmethod
    def _fields(ch):
        when = lambda v, all_day: {"date": _day(v)} if all_day else {"dateTime": _aware(v).isoformat(), "timeZone": tz_name()}
        b = {}
        if "title" in ch:
            b["summary"] = ch["title"]
        if "start" in ch:
            b["start"], b["end"] = when(ch["start"], ch.get("all_day")), when(ch["end"], ch.get("all_day"))
        for k, g in (("location", "location"), ("description", "description")):
            if k in ch:
                b[g] = ch[k]
        if "busy" in ch:
            b["transparency"] = "opaque" if ch["busy"] else "transparent"
        if "private" in ch:
            b["visibility"] = "private" if ch["private"] else "default"
        if "colour" in ch:
            b["colorId"] = PALETTE[ch["colour"]][1] if ch["colour"] in PALETTE else None
        if "guests" in ch:
            b["attendees"] = [{"email": g} for g in ch["guests"]]
        if ch.get("repeat") is not None and "repeat" in ch:
            b["recurrence"] = [f"RRULE:{ch['repeat']}"] if ch["repeat"] else []
        if ch.get("remind_minutes") is not None:
            b["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": ch["remind_minutes"]}]}
        if ch.get("conference") == "meet":
            b["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        elif ch.get("conference"):
            b["location"] = ch.get("location") or ch["conference"]
        return b

    def create_event(self, ev, notify=True):
        b = self._fields(ev)
        b.setdefault("extendedProperties", {"private": {"calsync_made": "1"}})
        q = {"conferenceDataVersion": "1", "sendUpdates": "all" if notify and ev.get("guests") else "none"}
        return http("POST", self._url("?" + urllib.parse.urlencode(q)), self.token(), b)["id"]

    def update_event(self, uid, ch, scope="this", notify=True):
        q = "?" + urllib.parse.urlencode({"conferenceDataVersion": "1", "sendUpdates": "all" if notify else "none"})
        it = self._one(uid)
        master_id = it.get("recurringEventId")
        if scope == "this" or not (master_id or it.get("recurrence")):
            return http("PATCH", self._url("/" + urllib.parse.quote(uid, safe="") + q), self.token(), self._fields(ch))
        master = self._one(master_id) if master_id else it
        mid = master["id"]
        if scope == "all":
            body = self._fields({k: v for k, v in ch.items() if k not in ("start", "end")})
            if "start" in ch:       # move every occurrence by as much as this one moved
                shift = _aware(ch["start"]) - _g_start(it)
                length = _aware(ch["end"]) - _aware(ch["start"])
                ms = _g_start(master) + shift
                body.update(self._fields({"start": ms, "end": ms + length, "all_day": ch.get("all_day")}))
            return http("PATCH", self._url("/" + urllib.parse.quote(mid, safe="") + q), self.token(), body)
        # this and following: end the series before this one, then start a new series here
        orig = _g_start({"start": it.get("originalStartTime") or it["start"]})
        rules = [r for r in master.get("recurrence", []) if r.startswith("RRULE:")]
        http("PATCH", self._url("/" + urllib.parse.quote(mid, safe="") + q), self.token(),
             {"recurrence": [_until(r, orig) for r in master.get("recurrence", [])]})
        new = {k: master[k] for k in ("summary", "description", "location", "transparency", "visibility", "colorId", "attendees") if k in master}
        new.update(start=it["start"], end=it["end"], recurrence=[re.sub(r";COUNT=\d+", "", r) for r in rules])
        new.update(self._fields(ch))
        return http("POST", self._url(q), self.token(), new)

    def delete_event(self, uid, scope="this", notify=True):
        q = "?sendUpdates=" + ("all" if notify else "none")
        it = self._one(uid)
        master_id = it.get("recurringEventId")
        if scope == "this" or not (master_id or it.get("recurrence")):
            return http("DELETE", self._url("/" + urllib.parse.quote(uid, safe="") + q), self.token())
        if scope == "all":
            return http("DELETE", self._url("/" + urllib.parse.quote(master_id or uid, safe="") + q), self.token())
        master = self._one(master_id)
        orig = _g_start({"start": it.get("originalStartTime") or it["start"]})
        http("PATCH", self._url("/" + urllib.parse.quote(master_id, safe="") + q), self.token(),
             {"recurrence": [_until(r, orig) for r in master.get("recurrence", [])]})

    def respond(self, uid, answer):
        it = self._one(uid)
        status = {"yes": "accepted", "maybe": "tentative", "no": "declined"}[answer]
        people = [dict(a, responseStatus=status) if a.get("self") else a for a in it.get("attendees", [])]
        http("PATCH", self._url("/" + urllib.parse.quote(uid, safe="") + "?sendUpdates=all"), self.token(), {"attendees": people})

    def changed_since(self, since):
        """Whether anything on this calendar changed after since (UTC 'Z'): one small request, for a quick check."""
        q = {"updatedMin": since, "showDeleted": "true", "maxResults": "1", "fields": "items(id)"}
        return bool(http("GET", self._url("?" + urllib.parse.urlencode(q)), self.token()).get("items"))

    def watch(self, address, token, channel):
        """Ask Google to call address when this calendar changes (needs a public https address). -> expiry (ms)."""
        r = http("POST", self._url("/watch"), self.token(), {"id": channel, "type": "web_hook", "address": address, "token": token,
                                                           "params": {"ttl": "604800"}})
        return {"id": channel, "resource": r.get("resourceId"), "expires": int(r.get("expiration", 0)) // 1000}

    def unwatch(self, ch):
        http("POST", f"{self.API}/channels/stop", self.token(), {"id": ch["id"], "resourceId": ch.get("resource")})


def _aware(v):
    """A datetime, date or ISO text -> an aware datetime (local zone when none is given)."""
    if isinstance(v, str):
        v = dt.datetime.fromisoformat(v.replace("Z", "+00:00")) if "T" in v else dt.date.fromisoformat(v)
    if not isinstance(v, dt.datetime):
        v = dt.datetime.combine(v, dt.time())
    return v if v.tzinfo else v.replace(tzinfo=local_tz())


def _day(v):
    return v[:10] if isinstance(v, str) else (v.date() if isinstance(v, dt.datetime) else v).isoformat()


def _g_start(it):
    s = it["start"]
    return _aware(s["dateTime"]) if "dateTime" in s else _aware(s["date"])


def _until(line, before):
    """A recurrence line that stops just before the datetime before. Other lines (EXDATE...) pass through."""
    if not line.startswith("RRULE:"):
        return line
    parts = [p for p in line[6:].split(";") if not p.startswith(("UNTIL=", "COUNT="))]
    stop = (before - dt.timedelta(seconds=1)).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "RRULE:" + ";".join(parts + [f"UNTIL={stop}"])


class OutlookCalendar:
    API = "https://graph.microsoft.com/v1.0/me"

    def __init__(self, account, cal_id):
        self.account, self.cal_id = account, cal_id
        self._token = None

    def token(self):
        if not self._token:
            self._token = microsoft_access_token(self.account)
        return self._token

    def _cal(self):
        return f"{self.API}/calendars/{urllib.parse.quote(self.cal_id, safe='')}"

    def list(self, tmin, tmax):
        out = []
        q = {"startDateTime": tmin, "endDateTime": tmax, "$top": "500",
             "$expand": f"singleValueExtendedProperties($filter=id eq '{MS_PROP}' or id eq '{MS_PROP_X}')"}
        url = self._cal() + "/calendarView?" + urllib.parse.urlencode(q)
        while url:
            r = http("GET", url, self.token(), headers={"Prefer": 'outlook.timezone="UTC", outlook.body-content-type="text"'})
            for it in r.get("value", []):
                ev = self._norm(it)
                if ev:
                    out.append(ev)
            url = r.get("@odata.nextLink")
        return out

    @staticmethod
    def _norm(it):
        if it.get("isCancelled"):
            return None
        all_day = bool(it.get("isAllDay"))
        def when(x):
            t = x["dateTime"][:19]
            return t[:10] if all_day else t + "Z"
        prop = lambda pid: next((p["value"] for p in it.get("singleValueExtendedProperties", []) if p.get("id", "").lower() == pid.lower()), None)
        answer = (it.get("responseStatus") or {}).get("response")
        mine = bool(it.get("isOrganizer", True)) or answer in (None, "organizer")
        return {
            "uid": it["id"], "ical": it.get("iCalUId"), "title": it.get("subject", ""),
            "start": when(it["start"]), "end": when(it["end"]), "all_day": all_day,
            "busy": it.get("showAs") not in ("free", "workingElsewhere"),
            "location": (it.get("location") or {}).get("displayName", ""),
            "private": it.get("sensitivity") in ("private", "confidential"),
            "declined": answer == "declined",
            "copy_of": prop(MS_PROP), "extra": prop(MS_PROP_X),
            "description": (it.get("body") or {}).get("content", "") if (it.get("body") or {}).get("contentType") == "text" else "",
            "conference": (it.get("onlineMeeting") or {}).get("joinUrl", "") or it.get("onlineMeetingUrl") or "",
            "attendees": [a["emailAddress"]["address"] for a in it.get("attendees", []) if (a.get("emailAddress") or {}).get("address")],
            "organizer": ((it.get("organizer") or {}).get("emailAddress") or {}).get("address", ""), "me_organizer": mine,
            "rsvp": "" if mine else {"accepted": "yes", "tentativelyAccepted": "maybe", "notResponded": "none", "none": "none", "declined": "no"}.get(answer, "none"),
            "kind": "ooo" if it.get("showAs") == "oof" else "",
            "colours": [c.lower() for c in it.get("categories", [])],
            "series": it.get("seriesMasterId"),
        }

    def _body(self, fields, copy_of, src_label):
        t = (lambda d: {"dateTime": d + "T00:00:00", "timeZone": "UTC"}) if fields["all_day"] else (lambda z: {"dateTime": z[:-1], "timeZone": "UTC"})
        props = [{"id": MS_PROP, "value": copy_of}] + ([{"id": MS_PROP_X, "value": fields["extra"]}] if fields.get("extra") else [])
        return {
            "subject": fields["title"], "location": {"displayName": fields["location"]},
            "body": {"contentType": "text", "content": copy_note(src_label, fields)},
            "start": t(fields["start"]), "end": t(fields["end"]), "isAllDay": fields["all_day"],
            "showAs": "busy" if fields["busy"] else "free", "isReminderOn": bool(fields.get("reminders")),
            "sensitivity": "private" if fields.get("private") else "normal",
            "categories": [fields["colour"].title()] if fields.get("colour") in PALETTE else [],
            "singleValueExtendedProperties": props,
        }

    def create(self, fields, copy_of, src_label):
        return http("POST", self._cal() + "/events", self.token(), self._body(fields, copy_of, src_label))["id"]

    def create_native(self, ev):
        """A real event (from quick add), not a copy: untagged, so the next sync copies it everywhere."""
        if ev["all_day"]:
            when = lambda d: {"dateTime": d + "T00:00:00", "timeZone": "UTC"}
        else:
            when = lambda t: {"dateTime": t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
        return http("POST", self._cal() + "/events", self.token(), {
            "subject": ev["title"], "start": when(ev["start"]), "end": when(ev["end"]), "isAllDay": ev["all_day"],
            "body": {"contentType": "text", "content": "Added with calsync quick add."}})["id"]

    def update(self, uid, fields, copy_of, src_label):
        http("PATCH", f"{self.API}/events/{urllib.parse.quote(uid, safe='')}", self.token(), self._body(fields, copy_of, src_label))

    def delete(self, uid):
        try:
            http("DELETE", f"{self.API}/events/{urllib.parse.quote(uid, safe='')}", self.token())
        except RuntimeError as e:
            if "-> 404" not in str(e):
                raise

    # real events, from the Calendar tab. Graph tells guests about changes itself; it has no way to stay quiet.

    def _ev(self, uid, method="GET", body=None, suffix=""):
        return http(method, f"{self.API}/events/{urllib.parse.quote(uid, safe='')}{suffix}", self.token(), body,
                    headers={"Prefer": 'outlook.timezone="UTC", outlook.body-content-type="text"'})

    def get(self, uid):
        return self._norm(self._ev(uid))

    @staticmethod
    def _fields(ch):
        def when(v, all_day):
            if all_day:
                return {"dateTime": _day(v) + "T00:00:00", "timeZone": tz_name()}
            return {"dateTime": _aware(v).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
        b = {}
        if "title" in ch:
            b["subject"] = ch["title"]
        if "start" in ch:
            b.update(start=when(ch["start"], ch.get("all_day")), end=when(ch["end"], ch.get("all_day")), isAllDay=bool(ch.get("all_day")))
        if "location" in ch:
            b["location"] = {"displayName": ch["location"]}
        if "description" in ch:
            b["body"] = {"contentType": "text", "content": ch["description"]}
        if "busy" in ch:
            b["showAs"] = "busy" if ch["busy"] else "free"
        if "private" in ch:
            b["sensitivity"] = "private" if ch["private"] else "normal"
        if "colour" in ch:
            b["categories"] = [ch["colour"].title()] if ch["colour"] in PALETTE else []
        if "guests" in ch:
            b["attendees"] = [{"emailAddress": {"address": g}, "type": "required"} for g in ch["guests"]]
        if ch.get("conference") == "teams":
            b.update(isOnlineMeeting=True, onlineMeetingProvider="teamsForBusiness")
        elif ch.get("conference") and ch["conference"] != "meet":
            b["location"] = {"displayName": ch.get("location") or ch["conference"]}
        if ch.get("repeat") and "start" in ch:
            b["recurrence"] = _graph_recurrence(ch["repeat"], _aware(ch["start"]))
        if ch.get("remind_minutes") is not None:
            b.update(isReminderOn=True, reminderMinutesBeforeStart=ch["remind_minutes"])
        return b

    def create_event(self, ev, notify=True):
        return http("POST", self._cal() + "/events", self.token(), self._fields(ev))["id"]

    def update_event(self, uid, ch, scope="this", notify=True):
        it = self._ev(uid)
        master = it.get("seriesMasterId")
        if scope == "this" or not master and it.get("type") != "seriesMaster":
            return self._ev(uid, "PATCH", self._fields(ch))
        mid = master or uid
        if scope == "all":
            body = self._fields({k: v for k, v in ch.items() if k not in ("start", "end")})
            if "start" in ch:
                m = self._ev(mid)
                shift = _aware(ch["start"]) - _aware(it["start"]["dateTime"][:19] + "+00:00")
                length = _aware(ch["end"]) - _aware(ch["start"])
                ms = _aware(m["start"]["dateTime"][:19] + "+00:00") + shift
                body.update(self._fields({"start": ms, "end": ms + length, "all_day": ch.get("all_day")}))
            return self._ev(mid, "PATCH", body)
        m = self._ev(mid)
        first = _aware(it["start"]["dateTime"][:19] + "+00:00")
        rec = m["recurrence"]
        self._ev(mid, "PATCH", {"recurrence": {"pattern": rec["pattern"], "range": dict(rec["range"], type="endDate",
                 endDate=(first.astimezone(local_tz()).date() - dt.timedelta(days=1)).isoformat())}})
        new = {k: m[k] for k in ("subject", "body", "location", "showAs", "sensitivity", "categories", "attendees") if k in m}
        new.update(start=it["start"], end=it["end"], recurrence={"pattern": rec["pattern"], "range": {"type": "noEnd", "startDate": first.astimezone(local_tz()).date().isoformat()}})
        new.update(self._fields({k: v for k, v in ch.items() if k != "repeat"}))
        return http("POST", self._cal() + "/events", self.token(), new)

    def delete_event(self, uid, scope="this", notify=True):
        it = self._ev(uid)
        master = it.get("seriesMasterId")
        if scope == "this" or not master:
            return self.delete(uid)
        if scope == "all":
            return self.delete(master)
        m = self._ev(master)
        first = _aware(it["start"]["dateTime"][:19] + "+00:00").astimezone(local_tz()).date()
        self._ev(master, "PATCH", {"recurrence": {"pattern": m["recurrence"]["pattern"],
                 "range": dict(m["recurrence"]["range"], type="endDate", endDate=(first - dt.timedelta(days=1)).isoformat())}})

    def respond(self, uid, answer):
        verb = {"yes": "accept", "maybe": "tentativelyAccept", "no": "decline"}[answer]
        self._ev(uid, "POST", {"sendResponse": True}, "/" + verb)

    def changed_since(self, since):
        q = {"$filter": f"lastModifiedDateTime ge {since}", "$top": "1", "$select": "id"}
        return bool(http("GET", self._cal() + "/events?" + urllib.parse.urlencode(q), self.token()).get("value"))

    def watch(self, address, token, channel):
        """A Graph subscription (needs a public https address); Graph allows about three days, then it's renewed."""
        until = (now_utc() + dt.timedelta(minutes=4000)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = http("POST", "https://graph.microsoft.com/v1.0/subscriptions", self.token(), {
            "changeType": "created,updated,deleted", "notificationUrl": address, "clientState": token,
            "resource": f"me/calendars/{self.cal_id}/events", "expirationDateTime": until})
        ends = dt.datetime.strptime(r["expirationDateTime"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return {"id": r["id"], "expires": int(ends.timestamp())}

    def unwatch(self, ch):
        http("DELETE", f"https://graph.microsoft.com/v1.0/subscriptions/{ch['id']}", self.token())


def _graph_recurrence(rule, start):
    """An RRULE (the few the Calendar tab offers) as Graph's recurrence."""
    p = dict(x.split("=", 1) for x in rule.split(";") if "=" in x)
    freq, every = p.get("FREQ", "WEEKLY"), int(p.get("INTERVAL", "1"))
    names = {"MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday", "FR": "friday", "SA": "saturday", "SU": "sunday"}
    local = start.astimezone(local_tz())
    pattern = {"type": {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "absoluteMonthly", "YEARLY": "absoluteYearly"}[freq], "interval": every}
    if freq == "WEEKLY":
        pattern["daysOfWeek"] = [names[d] for d in p.get("BYDAY", DAYS[local.weekday()][:2].upper()).split(",") if d in names]
    if freq in ("MONTHLY", "YEARLY"):
        pattern["dayOfMonth"] = local.day
    if freq == "YEARLY":
        pattern["month"] = local.month
    rng = {"type": "noEnd", "startDate": local.date().isoformat()}
    if "UNTIL" in p:
        rng.update(type="endDate", endDate=f"{p['UNTIL'][:4]}-{p['UNTIL'][4:6]}-{p['UNTIL'][6:8]}")
    elif "COUNT" in p:
        rng.update(type="numbered", numberOfOccurrences=int(p["COUNT"]))
    return {"pattern": pattern, "range": rng}


# ---------- read-only published calendars (.ics links, e.g. an Outlook account that blocks outside apps) ----------

WINDOWS_TZ = {  # Outlook writes Windows zone names into published calendars
    "dateline standard time": "Etc/GMT+12", "hawaiian standard time": "Pacific/Honolulu", "alaskan standard time": "America/Anchorage",
    "pacific standard time": "America/Los_Angeles", "us mountain standard time": "America/Phoenix", "mountain standard time": "America/Denver",
    "central standard time": "America/Chicago", "central standard time (mexico)": "America/Mexico_City", "canada central standard time": "America/Regina",
    "eastern standard time": "America/New_York", "us eastern standard time": "America/Indiana/Indianapolis", "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns", "e. south america standard time": "America/Sao_Paulo", "argentina standard time": "America/Buenos_Aires",
    "utc": "UTC", "coordinated universal time": "UTC", "gmt standard time": "Europe/London", "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin", "central europe standard time": "Europe/Budapest", "romance standard time": "Europe/Paris",
    "central european standard time": "Europe/Warsaw", "e. europe standard time": "Europe/Chisinau", "gtb standard time": "Europe/Athens",
    "fle standard time": "Europe/Kiev", "south africa standard time": "Africa/Johannesburg", "israel standard time": "Asia/Jerusalem",
    "russian standard time": "Europe/Moscow", "arabian standard time": "Asia/Dubai", "pakistan standard time": "Asia/Karachi",
    "india standard time": "Asia/Kolkata", "se asia standard time": "Asia/Bangkok", "china standard time": "Asia/Shanghai",
    "singapore standard time": "Asia/Singapore", "w. australia standard time": "Australia/Perth", "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul", "aus central standard time": "Australia/Darwin", "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
}


# The Windows zone names people are most likely to have. Anyone else sets their zone on the settings page.
WINDOWS_ZONES = {
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "Atlantic Standard Time": "America/Halifax",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "India Standard Time": "Asia/Kolkata", "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney", "New Zealand Standard Time": "Pacific/Auckland",
}


def windows_zone():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            return WINDOWS_ZONES.get(winreg.QueryValueEx(key, "TimeZoneKeyName")[0], "")
    except (ImportError, OSError):
        return ""


def tz_name():
    """The zone for quick-add text, agendas and .ics times that name none: CALSYNC_TZ, then config, then the system."""
    name = os.environ.get("CALSYNC_TZ") or load_json(CONFIG, {}).get("timezone")
    if not name:
        try:
            with open("/etc/timezone") as f:
                name = f.read().strip()
        except OSError:
            link = os.path.realpath("/etc/localtime")
            name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else windows_zone()
    return name or "UTC"


def local_tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz_name())
    except Exception:
        return dt.timezone.utc


def _unescape(v):
    import re
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def parse_ics(text):
    """-> list of VEVENT dicts: NAME -> [(params, value), ...]. Unfolds lines; ignores VTIMEZONE etc."""
    events, cur = [], None
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n ", "").replace("\n\t", "")  # joined in one go: a line folded millions of times can't slow this down
    for line in text.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *ps = head.split(";")
            params = dict(p.split("=", 1) for p in ps if "=" in p)
            cur.setdefault(name.upper(), []).append(({k.upper(): v.strip('"') for k, v in params.items()}, value))
    return events


def _ics_time(params, value):
    """-> (datetime, all_day). Timed values are timezone-aware."""
    from zoneinfo import ZoneInfo
    value = value.strip()
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return dt.datetime.strptime(value[:8], "%Y%m%d"), True
    t = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return t.replace(tzinfo=dt.timezone.utc), False
    tzid = params.get("TZID", "").rsplit("/freeassociation.sourceforge.net/", 1)[-1]
    try:
        return t.replace(tzinfo=ZoneInfo(WINDOWS_TZ.get(tzid.lower(), tzid))), False
    except Exception:
        return t.replace(tzinfo=local_tz()), False


def _ics_duration(v):
    import re
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return dt.timedelta(hours=1)
    sign, w, d, h, mi, se = m.groups()
    td = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(se or 0))
    return -td if sign == "-" else td


ICS_SECONDS = 60     # a calendar that takes longer to expand is reported, and its copies are left alone
_UNREADABLE = (ValueError, TypeError, OverflowError, KeyError, IndexError)   # what bad dates, rules and lengths raise


def ics_events(text, tmin, tmax, me=(), deadline=None):
    """Expand a calendar's VEVENTs into normalised instances inside [tmin, tmax] (UTC 'Z' strings).
    me: this account's own addresses, so a meeting it declined can be left out. Anyone can put an event on a calendar
    by sending an invite, so one that can't be read is skipped rather than stopping the whole calendar, and a rule that
    repeats every minute or second is skipped because expanding it from years back would hang the run."""
    from dateutil.rrule import rruleset, rrulestr
    deadline = deadline or time.monotonic() + ICS_SECONDS
    skipped = 0
    lo = dt.datetime.fromisoformat(tmin.replace("Z", "+00:00"))
    hi = dt.datetime.fromisoformat(tmax.replace("Z", "+00:00"))
    first = lambda ev, k: ev[k][0] if k in ev else ({}, "")
    masters, overrides = [], {}
    for ev in parse_ics(text):
        if "DTSTART" not in ev or "UID" not in ev:
            skipped += _may_be_in_window(ev, lo, hi)
            continue
        if "RECURRENCE-ID" in ev:
            try:
                rid, _ = _ics_time(*first(ev, "RECURRENCE-ID"))
            except _UNREADABLE:
                skipped += 1
                continue
            overrides[(first(ev, "UID")[1], rid.isoformat())] = ev
        else:
            masters.append(ev)

    def instance(ev, start, all_day, orig):
        uid = first(ev, "UID")[1]
        if "DTEND" in ev:
            s0, _ = _ics_time(*first(ev, "DTSTART"))
            e0, _ = _ics_time(*first(ev, "DTEND"))
            length = e0 - s0
        else:
            length = _ics_duration(first(ev, "DURATION")[1]) if "DURATION" in ev else dt.timedelta(days=1 if all_day else 0)
        end = start + length
        if first(ev, "STATUS")[1].upper() == "CANCELLED":
            return None
        if all_day:
            if end.date() < lo.date() or start.date() > hi.date():
                return None
            fs, fe = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        else:
            if end < lo or start > hi:
                return None
            fs = start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fe = end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        busy = first(ev, "X-MICROSOFT-CDO-BUSYSTATUS")[1].upper() not in ("FREE", "WORKINGELSEWHERE") and first(ev, "TRANSP")[1].upper() != "TRANSPARENT"
        tag = _unescape(first(ev, ICS_TAG)[1]) or (f"?|{uid}" if uid.startswith(COPY_UID) or COPY_NOTE.match(_unescape(first(ev, "DESCRIPTION")[1])) else None)
        mine = [p.get("PARTSTAT", "").upper() for p, v in ev.get("ATTENDEE", []) if v.lower().removeprefix("mailto:") in me]
        organizer = first(ev, "ORGANIZER")[1].lower().removeprefix("mailto:")
        desc = _unescape(first(ev, "DESCRIPTION")[1])
        conf = next((_unescape(first(ev, k)[1]) for k in ("X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL", "URL") if first(ev, k)[1]), "")
        conf = conf or next(iter(re.findall(r"https://[^\s<>\"]*(?:zoom\.us/j|meet\.google\.com|teams\.microsoft\.com/l/meetup-join|webex\.com)[^\s<>\"]*", desc)), "")
        cats = [c.strip().lower() for _, v in ev.get("CATEGORIES", []) for c in _unescape(v).split(",") if c.strip()]
        colour = first(ev, "COLOR")[1].strip().lower()
        return {"uid": f"{uid}|{orig}", "ical": uid, "title": _unescape(first(ev, "SUMMARY")[1]),
                "start": fs, "end": fe, "all_day": all_day, "busy": busy,
                "location": _unescape(first(ev, "LOCATION")[1]),
                "private": first(ev, "CLASS")[1].upper() in ("PRIVATE", "CONFIDENTIAL"),
                "declined": "DECLINED" in mine, "copy_of": tag, "extra": first(ev, ICS_TAG_X)[1] or None,
                "description": desc, "conference": conf,
                "attendees": [v.lower().removeprefix("mailto:") for p, v in ev.get("ATTENDEE", []) if v.lower().removeprefix("mailto:") not in me],
                "organizer": organizer, "me_organizer": not mine or organizer in me,
                "rsvp": "" if not mine or organizer in me else {"ACCEPTED": "yes", "TENTATIVE": "maybe", "DECLINED": "no"}.get(mine[0], "none"),
                "kind": "ooo" if first(ev, "X-MICROSOFT-CDO-BUSYSTATUS")[1].upper() == "OOF" else ("birthday" if "birthday" in cats else ""),
                "colours": cats + ([colour] if colour in PALETTE else []),
                "series": uid if "RRULE" in ev or "RECURRENCE-ID" in ev else None}

    def expand(ev):
        uid = first(ev, "UID")[1]
        start, all_day = _ics_time(*first(ev, "DTSTART"))
        if "RRULE" not in ev and "RDATE" not in ev:
            return [instance(overrides.pop((uid, start.isoformat()), ev), start, all_day, start.isoformat())]
        rs = rruleset()
        if "RRULE" not in ev:
            rs.rdate(start)
        for params, val in ev.get("RDATE", []):
            for v in val.split(","):
                rs.rdate(_ics_time(params, v)[0])
        for _, rule in ev.get("RRULE", []):
            if re.search(r"FREQ\s*=\s*(SECONDLY|MINUTELY)", rule, re.I):
                raise ValueError("repeats too often")
            if all_day:  # dateutil wants UNTIL to match DTSTART's (naive) kind
                rule = ";".join(p[:14] if p.startswith("UNTIL=") else p for p in rule.split(";"))
            elif "UNTIL=" in rule and not rule.split("UNTIL=")[1].split(";")[0].endswith("Z"):
                rule = rule.replace(rule.split("UNTIL=")[1].split(";")[0], rule.split("UNTIL=")[1].split(";")[0][:15] + "Z")
            rs.rrule(rrulestr(rule, dtstart=start))
        for params, val in ev.get("EXDATE", []):
            for v in val.split(","):
                ex, _ = _ics_time(params, v)
                rs.exdate(ex)
        span_lo = (lo - dt.timedelta(days=2)).replace(tzinfo=None) if all_day else lo - dt.timedelta(days=2)
        span_hi = hi.replace(tzinfo=None) if all_day else hi
        found = []
        for occ in rs.between(span_lo, span_hi, inc=True):
            key = occ.isoformat()
            src = overrides.pop((uid, key), None)
            if src is not None:
                s2, ad2 = _ics_time(*first(src, "DTSTART"))
                found.append(instance(src, s2, ad2, key))
            else:
                found.append(instance(ev, occ, all_day, key))
        return found

    out = []
    for ev in masters:
        if time.monotonic() > deadline:
            raise RuntimeError("this calendar has too many repeating events to read")
        try:
            out += [inst for inst in expand(ev) if inst]
        except _UNREADABLE:
            skipped += _may_be_in_window(ev, lo, hi)
    for (uid, key), ev in overrides.items():  # moved occurrences whose original slot fell outside the window
        try:
            s2, ad2 = _ics_time(*first(ev, "DTSTART"))
            inst = instance(ev, s2, ad2, key)
        except _UNREADABLE:
            skipped += 1
            continue
        if inst:
            out.append(inst)
    if skipped:
        _skipped.count = getattr(_skipped, "count", 0) + skipped
        print(f"calsync: skipped {skipped} event(s) on a calendar because they couldn't be read", file=sys.stderr)
    return out


def _may_be_in_window(ev, lo, hi):
    """1 if an event calsync can't read could fall in [lo, hi]. One dead event from years ago says nothing about this week."""
    try:
        start, _ = _ics_time(*ev["DTSTART"][0])
        start = start if start.tzinfo else start.replace(tzinfo=lo.tzinfo)     # an all-day start has no zone
        repeats = "RRULE" in ev or "RDATE" in ev
        return 0 if not repeats and not lo - dt.timedelta(days=366) <= start <= hi else 1
    except (*_UNREADABLE, AttributeError):
        return 1


_skipped = threading.local()


def take_skipped():
    """How many events this thread couldn't read since it last asked. A list that left some out isn't the whole picture."""
    count = getattr(_skipped, "count", 0)
    _skipped.count = 0
    return count


def clean_ics_url(url):
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
        raise RuntimeError("a calendar link has to start with https://")
    if re.search(r"[\s\x00-\x1f\x7f]", url):  # urllib would quote the whole link, secret part included, in its error
        raise RuntimeError("a calendar link can't have spaces in it")
    return url


DUE_LENGTH = dt.timedelta(minutes=30)


def due_items(evs, tmin, tmax, only=None, free=False, for_sync=True):
    """Shape a due-date feed, such as Canvas assignments. --only keeps items whose UID holds that text; --free stops
    them marking anyone busy, since a due date isn't a meeting. A due time with no length ends at the deadline, so it
    can be seen. All-day items are held to what Google will list back: it hides one once its day is over and doesn't
    reach one just past the window, and ics_events keeps both, which would make a new copy on every run. That trim
    is only for sync: anything that shows a calendar to a person (agendas, the Calendar tab, agents) asks with
    for_sync=False through list_for_people(), or items due in the last two days of what it shows would go missing."""
    lo = dt.datetime.fromisoformat(tmin.replace("Z", "+00:00"))
    last = dt.datetime.fromisoformat(tmax.replace("Z", "+00:00")).date() - dt.timedelta(days=2)
    out = []
    for ev in evs:
        if only and only not in (ev.get("ical") or ""):
            continue
        if ev["all_day"]:
            if dt.datetime.fromisoformat(ev["end"]).replace(tzinfo=local_tz()) <= lo or (for_sync and dt.date.fromisoformat(ev["start"]) >= last):
                continue
        elif ev["start"] == ev["end"]:
            due = dt.datetime.fromisoformat(ev["end"].replace("Z", "+00:00"))
            ev = {**ev, "start": (due - DUE_LENGTH).strftime("%Y-%m-%dT%H:%M:%SZ")}
        out.append({**ev, "busy": False} if free else ev)
    return out


class IcsCalendar:
    read_only = True

    MAX_BYTES = 50 * 1024 * 1024

    def __init__(self, url, only=None, free=False):
        self.url, self.only, self.free = clean_ics_url(url), only, free

    def list(self, tmin, tmax, for_sync=True):
        req = urllib.request.Request(self.url, headers={"User-Agent": "calsync"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:  # reading only part would look like the rest of the events were deleted
            raise RuntimeError("this calendar is over 50 MB, so calsync didn't read it")
        text = raw.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            raise RuntimeError("link did not return a calendar")
        evs = ics_events(text, tmin, tmax)
        return due_items(evs, tmin, tmax, self.only, self.free, for_sync) if self.only or self.free else evs


def list_for_people(adapter, tmin, tmax):
    """A calendar's events for showing, not for syncing: a due-date feed keeps its last two days (see due_items)."""
    return adapter.list(tmin, tmax, for_sync=False) if isinstance(adapter, IcsCalendar) else adapter.list(tmin, tmax)


# ---------- CalDAV: iCloud, Fastmail, Yahoo, AOL, Zoho, Nextcloud and anything else that speaks it ----------

CALDAV_SERVERS = {  # name, server (None: you type your own), where the password comes from
    "icloud": ("Apple iCloud", "https://caldav.icloud.com/", "Use your Apple Account email, and an app-specific password from account.apple.com, under Sign-In and Security. Your normal Apple password won't work."),
    "fastmail": ("Fastmail", "https://caldav.fastmail.com/", "Use your Fastmail address, and an app password from Settings, Privacy &amp; Security, with Calendars (CalDAV) access."),
    "yahoo": ("Yahoo Calendar", "https://caldav.calendar.yahoo.com/", "Use your Yahoo address, and an app password from Account Security."),
    "aol": ("AOL Calendar", "https://caldav.aol.com/", "Use your AOL address, and an app password from Account Security."),
    "zoho": ("Zoho Calendar", "https://calendar.zoho.com/", "Use your Zoho address, and an app password from accounts.zoho.com, under Security. Accounts outside the US use Another CalDAV server with their own address, such as calendar.zoho.eu."),
    "nextcloud": ("Nextcloud", None, "Use your Nextcloud address, and an app password from Personal settings, Security."),
    "other": ("CalDAV server", None, "Use the address, username and password your calendar provider gives for CalDAV. Most providers ask for an app password."),
}
DAV, CALNS = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
_XML = {"Content-Type": "application/xml; charset=utf-8"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # DavClient follows redirects itself, and checks each one before sending a password
        return None


def _local_host(host):
    if "." not in host or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _same_site(typed, other):
    """Where a password may follow: the host it was typed for, or a host under it. Sharing a last two labels isn't
    enough, since that's also true of strangers on co.uk, duckdns.org or a private network. Only the built-in providers
    move you between their own hosts, as iCloud does to pNN-caldav.icloud.com."""
    if other == typed or other.endswith("." + typed):
        return True
    site = typed.split(".")[-2:]
    return site == other.split(".")[-2:] and ".".join(site) in {".".join(urllib.parse.urlparse(c[1]).hostname.split(".")[-2:]) for c in CALDAV_SERVERS.values() if c[1]}


def clean_dav_url(url):
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        raise RuntimeError("that doesn't look like a server address")
    if p.scheme.lower() == "http" and not _local_host(p.hostname):
        raise RuntimeError("a password only goes over https://; http:// is for a server on your own network")
    return url


class DavClient:
    MAX_BYTES = 50 * 1024 * 1024

    def __init__(self, server, username, password):
        self.server = clean_dav_url(server)
        self.username, self.password = username, password
        self.digest, self.count = None, 0
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _trusted(self, url):
        """A password only goes back to the site it was typed for, and never from https:// down to http://."""
        a, b = urllib.parse.urlparse(self.server), urllib.parse.urlparse(url)
        return _same_site(a.hostname or "", b.hostname or "") and (b.scheme.lower() == "https" or a.scheme.lower() == "http")

    def _authorization(self, method, url):
        if not self.digest:
            return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        d = self.digest
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        p = urllib.parse.urlparse(url)
        path = p.path + (f"?{p.query}" if p.query else "") or "/"
        ha1, ha2 = md5(f"{self.username}:{d['realm']}:{self.password}"), md5(f"{method}:{path}")
        self.count += 1
        nc, cnonce = f"{self.count:08x}", os.urandom(8).hex()
        nonce, realm = d["nonce"], d["realm"]
        head = f'username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{path}"'
        if d.get("opaque"):
            head += ', opaque="%s"' % d["opaque"]
        if "auth" in d.get("qop", "").replace(" ", "").split(","):
            answer = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            return f'Digest {head}, qop=auth, nc={nc}, cnonce="{cnonce}", response="{answer}"'
        answer = md5(f"{ha1}:{nonce}:{ha2}")
        return f'Digest {head}, response="{answer}"'

    def _challenge(self, headers):
        """Some servers (Baikal by default) only take Digest. Returns True if a new challenge was picked up."""
        for value in headers.get_all("WWW-Authenticate") or []:
            if value.lower().startswith("digest "):
                d = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^\s,]+)', value)}
                if d.get("nonce") and d.get("nonce") != (self.digest or {}).get("nonce"):
                    self.digest, self.count = d, 0
                    return True
        return False

    def request(self, method, url, body=None, headers=None, depth=None):
        """-> (status, raw bytes, final url). Never raises for an HTTP status, and never puts the password in a message."""
        h = {"User-Agent": "calsync", **(headers or {})}
        if depth is not None:
            h["Depth"] = depth
        data = body.encode() if body is not None else None
        hops = retries = 0
        while True:
            if not self._trusted(url):
                raise RuntimeError("the server sent calsync to another site, so it stopped to keep your password safe")
            h["Authorization"] = self._authorization(method, url)
            try:
                with self.opener.open(urllib.request.Request(url, data=data, method=method, headers=h), timeout=60) as r:
                    return r.status, r.read(self.MAX_BYTES), url
            except urllib.error.HTTPError as e:
                raw = e.read(self.MAX_BYTES)
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location") and hops < 5:
                    url, hops = urllib.parse.urljoin(url, e.headers["Location"]), hops + 1
                elif e.code == 401 and self._challenge(e.headers):
                    continue
                elif e.code in (429, 500, 502, 503) and retries < 3:
                    time.sleep(2 ** retries * 2)
                    retries += 1
                else:
                    return e.code, raw, url

    def propfind(self, url, props, depth="0"):
        body = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f"<d:prop>{props}</d:prop></d:propfind>")
        status, raw, final = self.request("PROPFIND", url, body, _XML, depth)
        if status == 401:
            raise RuntimeError("the server turned down that username or password")
        return (_multistatus(raw) if status == 207 else None), final


def _multistatus(raw):
    """-> [(href, {property tag: element})], keeping only the properties the server actually has."""
    out = []
    for r in ET.fromstring(raw).iter(DAV + "response"):
        props = {}
        for ps in r.findall(DAV + "propstat"):
            prop = ps.find(DAV + "prop")
            if prop is not None and " 200 " in (ps.findtext(DAV + "status") or " 200 ") + " ":
                props.update((p.tag, p) for p in prop)
        out.append(((r.findtext(DAV + "href") or "").strip(), props))
    return out


def caldav_discover(client):
    """-> (urls to list calendars from, this account's addresses). Follows RFC 6764: the address, /.well-known/caldav, the site root."""
    p = urllib.parse.urlparse(client.server)
    origin = f"{p.scheme}://{p.netloc}"
    for url in dict.fromkeys((client.server, origin + "/.well-known/caldav", origin + "/")):
        rows, final = client.propfind(url, "<d:current-user-principal/><d:resourcetype/>")
        if not rows:
            continue
        principal = rows[0][1].get(DAV + "current-user-principal")
        href = principal.findtext(DAV + "href") if principal is not None else None
        if href:
            break
        kinds = rows[0][1].get(DAV + "resourcetype")
        if kinds is not None and kinds.find(CALNS + "calendar") is not None:  # a calendar's own address, on a server with no principals
            return [final], []
    else:
        raise RuntimeError("no calendar server answered at that address")
    rows, final = client.propfind(urllib.parse.urljoin(final, href), "<c:calendar-home-set/><c:calendar-user-address-set/>")
    if not rows:
        raise RuntimeError("the server didn't say where your calendars are")
    props = rows[0][1]
    hrefs = lambda tag: [(e.text or "").strip() for e in (props[tag].iter(DAV + "href") if tag in props else [])]
    homes = [urllib.parse.urljoin(final, h) for h in hrefs(CALNS + "calendar-home-set")]
    if not homes:
        raise RuntimeError("the server didn't say where your calendars are")
    return homes, [a.lower().removeprefix("mailto:") for a in hrefs(CALNS + "calendar-user-address-set")]


def caldav_calendars(client, homes):
    """-> [{id, name, writable}]: event calendars only, so a Reminders list never shows up."""
    out = []
    for home in homes:
        rows, final = client.propfind(home, "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/><d:current-user-privilege-set/>", "1")
        for href, props in rows or []:
            kinds, comps, rights = (props.get(k) for k in (DAV + "resourcetype", CALNS + "supported-calendar-component-set", DAV + "current-user-privilege-set"))
            if kinds is None or kinds.find(CALNS + "calendar") is None:
                continue
            if comps is not None and "VEVENT" not in [c.get("name") for c in comps]:
                continue
            url = urllib.parse.urljoin(final, href)
            name = props[DAV + "displayname"].text if DAV + "displayname" in props else None
            can = {e.tag for e in rights.iter()} if rights is not None else None
            out.append({"id": url, "name": name or url.rstrip("/").rsplit("/", 1)[-1],
                        "writable": can is None or bool(can & {DAV + "write", DAV + "write-content", DAV + "all"})})
    return out


def caldav_connect(label, server, username, password, service=""):
    """Check the sign-in by finding the calendars, then store it. Returns the calendars."""
    client = DavClient(server, username, password)
    homes, addresses = caldav_discover(client)
    found = caldav_calendars(client, homes)
    if not found:
        raise RuntimeError("signed in, but this account has no calendars")
    _save_account(label, {"provider": "caldav", "service": service, "server": client.server, "username": username,
                          "password": password, "homes": homes, "addresses": addresses})
    return found


class CalDavCalendar:
    def __init__(self, acct, cal_url):
        self.client = DavClient(acct["server"], acct["username"], acct["password"])
        self.url = cal_url.rstrip("/") + "/"
        self.me = set(acct.get("addresses") or []) | {acct["username"].lower()}
        self.tag_checked = False

    def list(self, tmin, tmax):
        z = lambda s: s.replace("-", "").replace(":", "")
        body = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{z(tmin)}" end="{z(tmax)}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        status, raw, final = self.client.request("REPORT", self.url, body, _XML, "1")
        if status != 207:
            raise RuntimeError(f"reading this calendar -> {status}")
        out, deadline = [], time.monotonic() + ICS_SECONDS
        for href, props in _multistatus(raw):
            data = props.get(CALNS + "calendar-data")
            if data is None or not data.text:
                is_folder = href.endswith("/") or urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/") == urllib.parse.unquote(urllib.parse.urlsplit(self.url).path).rstrip("/")
                if data is not None or not is_folder:       # the calendar's own entry has no data, and isn't an event
                    _skipped.count = getattr(_skipped, "count", 0) + 1
                continue
            for ev in ics_events(data.text, tmin, tmax, self.me, deadline):
                ev["href"] = urllib.parse.urljoin(final, href)
                if ev["copy_of"]:
                    ev["uid"] = ev["href"]  # what update and delete need
                out.append(ev)
        return out

    # real events, from the Calendar tab. Guests aren't invited from here: CalDAV servers differ too much in how they send invites.

    def _mine(self, href):
        href = urllib.parse.urljoin(self.url, href or "")
        if not href.startswith(self.url) or not href.endswith(".ics"):
            raise RuntimeError("that event isn't on this calendar")
        status, raw, _ = self.client.request("GET", href)
        if status != 200:
            raise RuntimeError(f"reading this event -> {status}")
        return href, raw.decode("utf-8", errors="replace")

    def create_event(self, ev, notify=True):
        uid = f"made-{uuid.uuid4().hex}@calsync"
        href = f"{self.url}{uid.split('@')[0]}.ics"
        self._put(href, [f"UID:{uid}", *_vevent_lines(ev)])
        return href

    def update_event(self, uid, ch, scope="this", notify=True, href=None):
        href, text = self._mine(href)
        blocks = _vevents(text)
        orig = uid.split("|", 1)[1] if "|" in uid else ""
        master = _master(blocks)
        repeats = bool(_prop(master, "RRULE") or _prop(master, "RDATE"))
        if not repeats or scope == "all":
            changes = dict(ch)
            if repeats and "start" in ch and orig:
                shift = _aware(ch["start"]) - _aware(orig)
                length = _aware(ch["end"]) - _aware(ch["start"])
                ms = _ics_time(*_param_value(_prop(master, "DTSTART")))[0]
                ms = _aware(ms) + shift
                changes.update(start=ms, end=ms + length)
            blocks[blocks.index(master)] = _vevent_edit(master, changes)
        elif scope == "this":
            o = _aware(orig)
            rid = _ics_when("RECURRENCE-ID", o.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if not ch.get("all_day") else o.date().isoformat(), ch.get("all_day"))
            old = next((b for b in blocks if b[0] == "BEGIN:VEVENT" and _prop(b, "RECURRENCE-ID") and _aware(_ics_time(*_param_value(_prop(b, "RECURRENCE-ID")))[0]) == o), None)
            base = old or [l for l in master[:-1] if not l.startswith(("RRULE", "RDATE", "EXDATE"))] + [rid, master[-1]]
            if not old and "start" not in ch:
                length = _aware(_ics_time(*_param_value(_prop(master, "DTEND")))[0]) - _aware(_ics_time(*_param_value(_prop(master, "DTSTART")))[0]) if _prop(master, "DTEND") else dt.timedelta(hours=1)
                ch = dict(ch, start=o, end=o + length)
            edited = _vevent_edit(base, ch)
            blocks = [b for b in blocks if b is not old] + [edited]
        else:                   # this and following: stop the old series before this one, start a new one here
            o = _aware(orig)
            blocks[blocks.index(master)] = [_until(l, o) if l.startswith("RRULE") else l for l in master]
            new_uid = f"made-{uuid.uuid4().hex}@calsync"
            fresh = [l for l in master if not l.startswith(("UID", "EXDATE", "RECURRENCE-ID"))]
            fresh.insert(1, f"UID:{new_uid}")
            fresh = [re.sub(r";COUNT=\d+", "", l) if l.startswith("RRULE") else l for l in fresh]
            if "start" not in ch:
                length = _aware(_ics_time(*_param_value(_prop(master, "DTEND")))[0]) - _aware(_ics_time(*_param_value(_prop(master, "DTSTART")))[0]) if _prop(master, "DTEND") else dt.timedelta(hours=1)
                ch = dict(ch, start=o, end=o + length)
            self._put_raw(f"{self.url}{new_uid.split('@')[0]}.ics", [b for b in blocks if b[0] != "BEGIN:VEVENT"] + [_vevent_edit(fresh, ch)])
        self._put_raw(href, blocks)

    def delete_event(self, uid, scope="this", notify=True, href=None):
        href, text = self._mine(href)
        blocks = _vevents(text)
        master = _master(blocks)
        orig = uid.split("|", 1)[1] if "|" in uid else ""
        if scope == "all" or not (_prop(master, "RRULE") or _prop(master, "RDATE")) or not orig:
            return self.delete(href)
        o = _aware(orig)
        if scope == "this":
            master.insert(-1, "EXDATE:" + o.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        else:
            blocks[blocks.index(master)] = [_until(l, o) if l.startswith("RRULE") else l for l in master]
        self._put_raw(href, blocks)

    def _put_raw(self, href, blocks):
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//calsync//calendar//EN", "CALSCALE:GREGORIAN", *[l for b in blocks for l in b], "END:VCALENDAR"]
        body = "".join(part + "\r\n" for line in lines for part in _ics_fold(line))
        status, raw, _ = self.client.request("PUT", href, body, {"Content-Type": "text/calendar; charset=utf-8"})
        if status not in (200, 201, 204):
            raise RuntimeError(f"writing to this calendar -> {status} {raw[:200].decode(errors='replace')}")

    def changed_since(self, since):
        return True     # CalDAV has no cheap check that every server supports; it syncs on the interval

    def _put(self, href, lines):
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//calsync//sync//EN", "CALSCALE:GREGORIAN", "BEGIN:VEVENT", "DTSTAMP:" + stamp, *lines, "END:VEVENT", "END:VCALENDAR"]
        body = "".join(part + "\r\n" for line in lines for part in _ics_fold(line))
        status, raw, _ = self.client.request("PUT", href, body, {"Content-Type": "text/calendar; charset=utf-8"})
        if status not in (200, 201, 204):
            raise RuntimeError(f"writing to this calendar -> {status} {raw[:200].decode(errors='replace')}")

    def _copy(self, href, fields, copy_of, src_label):
        lines = [f"UID:{self._copy_uid(copy_of)}", _ics_when("DTSTART", fields["start"], fields["all_day"]), _ics_when("DTEND", fields["end"], fields["all_day"]),
                 "SUMMARY:" + _ics_escape(fields["title"]), "TRANSP:" + ("OPAQUE" if fields["busy"] else "TRANSPARENT"),
                 "DESCRIPTION:" + _ics_escape(copy_note(src_label, fields)),
                 f"{ICS_TAG}:{_ics_escape(copy_of)}"]
        if fields["location"]:
            lines.append("LOCATION:" + _ics_escape(fields["location"]))
        if fields.get("extra"):
            lines.append(f"{ICS_TAG_X}:{fields['extra']}")
        if fields.get("private"):
            lines.append("CLASS:PRIVATE")
        if fields.get("colour") in PALETTE:
            lines.append(f"COLOR:{fields['colour']}")
        if fields.get("reminders"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT15M", "END:VALARM"]
        self._put(href, lines)

    @staticmethod
    def _copy_uid(copy_of):
        return COPY_UID + hashlib.sha256(copy_of.encode()).hexdigest()[:32]

    def create(self, fields, copy_of, src_label):
        href = f"{self.url}{self._copy_uid(copy_of)}.ics"
        self._copy(href, fields, copy_of, src_label)
        if not self.tag_checked:  # without the tag a copy can't be matched to its original, so stop before making any more
            status, raw, _ = self.client.request("GET", href)
            if status == 200 and ICS_TAG.encode() not in raw.upper():
                self.delete(href)
                raise RuntimeError("this server drops calsync's tag, so calsync can read this calendar but not write to it; turn on Never write to this calendar")
            self.tag_checked = True
        return href

    def update(self, uid, fields, copy_of, src_label):
        self._copy(uid, fields, copy_of, src_label)

    def create_native(self, ev):
        """A real event (from quick add), not a copy: untagged, so the next sync copies it everywhere."""
        z = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        when = (lambda v: v) if ev["all_day"] else z
        uid = f"quick-{uuid.uuid4().hex}@calsync"
        href = f"{self.url}{uid.split('@')[0]}.ics"
        self._put(href, [f"UID:{uid}", _ics_when("DTSTART", when(ev["start"]), ev["all_day"]), _ics_when("DTEND", when(ev["end"]), ev["all_day"]),
                         "SUMMARY:" + _ics_escape(ev["title"]), "DESCRIPTION:Added with calsync quick add."])
        return href

    def delete(self, uid):
        status, raw, _ = self.client.request("DELETE", uid)
        if status not in (200, 202, 204, 404, 410):
            raise RuntimeError(f"deleting from this calendar -> {status} {raw[:200].decode(errors='replace')}")


def make_adapter(cfg):
    acct = load_json(os.path.join(SECRETS, "accounts.json"), {}).get(cfg["account"])
    if not acct:
        raise RuntimeError(f"account {cfg['account']!r} is not signed in")
    if acct["provider"] == "ics":
        return IcsCalendar(acct["url"], cfg.get("only"), cfg.get("free", False))
    if acct["provider"] == "caldav":
        return CalDavCalendar(acct, cfg["calendar_id"])
    cls = {"google": GoogleCalendar, "outlook": OutlookCalendar}[acct["provider"]]
    return cls(cfg["account"], cfg["calendar_id"])


# ---------- the sync engine ----------

def _canceled(ev):
    # Outlook leaves "Canceled: <title>" meetings on attendees' calendars until they're removed by hand.
    return (ev.get("title") or "").lower().startswith(("canceled:", "cancelled:"))


def sync(cals, adapters, dry_run=False, force=False, log=print, now=None, cfg=None, decisions=None, pair=None, refresh=False):
    """cals: list of calendar configs; adapters: name -> adapter. Returns a summary dict.
    cfg: the whole config, for the rules (cfg["rules"]), the window and kept copies; without it the older per-calendar
    settings apply. decisions: a list that gets, for every source event and every calendar it could go to, whether it
    was copied and why (the Preview's "why"). pair: "SRC>DST" to sync only that direction; refresh rewrites its copies."""
    now = now or now_utc()
    cfg = dict(cfg or {}, calendars=cals)
    tmin, tmax = sync_window(cfg, now)
    only_src, only_dst = pair.split(">", 1) if pair else (None, None)
    listed, failed = {}, set()
    for c in cals:
        if pair and c["name"] not in (only_src, only_dst):
            continue
        try:
            listed[c["name"]] = adapters[c["name"]].list(tmin, tmax)
        except Exception as e:  # one bad calendar must not stop the rest, nor delete its copies
            failed.add(c["name"])
            log(f"! {c['name']}: list failed: {e}")
    labels = {c["name"]: c.get("label") or c["name"] for c in cals}
    skips = skipped(cfg)    # occurrences the person marked "Skip this one": not copied, so their copies go
    kept = set(cfg.get("kept_sources", []))     # calendars taken out with "keep the copies": their copies are left alone
    sources, why_not = {}, []
    for n, evs in listed.items():
        sources[n] = []
        for e in evs:
            why = not_a_source(e, n, skips)
            if why is None:
                sources[n].append(e)
            elif not e.get("copy_of"):
                why_not.append((n, e, why))
    summary = {"created": 0, "updated": 0, "deleted": 0, "skipped_deletes": 0, "failed": sorted(failed), "errors": 0, "pairs": {}}

    def tell(src, dst, ev, copied, reason, action=""):
        if decisions is not None:
            decisions.append({"src": src, "dst": dst, "title": ev.get("title") or "(no title)", "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"],
                              "private": bool(ev.get("private")), "copied": copied, "reason": reason, "action": action})

    def count(src, dst, what):
        p = summary["pairs"].setdefault(f"{src}>{dst}", {"created": 0, "updated": 0, "deleted": 0, "errors": 0})
        p[what] += 1

    for dst in cals:
        name = dst["name"]
        if pair and name != only_dst:
            continue
        unwritable = dst.get("read_only") or getattr(adapters[name], "read_only", False)
        if unwritable or name in failed:
            for src, evs in sources.items():
                if src != name and decisions is not None:
                    for ev in evs:
                        tell(src, name, ev, False, f"{labels[name]} is never written to" if unwritable else f"{labels[name]} couldn't be read just now")
            continue
        ad = adapters[name]
        # A meeting you were invited to on several accounts already sits on dst natively: don't add a second copy.
        native = {e["ical"] for e in listed[name] if not e["copy_of"] and e.get("ical")}
        desired, rules = {}, {}
        for src, evs in sources.items():
            if src == name or (pair and src != only_src):
                continue
            rule = rules[src] = rule_for(src, dst, cfg)
            for ev in evs:
                if not rule["enabled"]:
                    tell(src, name, ev, False, f"copying from {labels[src]} to {labels[name]} is off")
                    continue
                if ev.get("ical") and ev["ical"] in native:
                    tell(src, name, ev, False, f"it's already on {labels[name]} through an invite")
                    continue
                why = filtered_out(ev, rule)
                if why:
                    tell(src, name, ev, False, why)
                    continue
                fields = render(ev, src, dst, rule, labels[src])
                desired[f"{src}|{ev['uid']}"] = (fields, fields.pop("label"), ev)
        existing = {}
        dupes = []
        for ev in listed[name]:
            if ev["copy_of"]:
                if pair and ev["copy_of"].split("|", 1)[0] != only_src:
                    continue
                if ev["copy_of"] in existing:
                    dupes.append(ev)
                else:
                    existing[ev["copy_of"]] = ev

        def leave_alone(key):
            src = key.split("|", 1)[0]
            r = rules.get(src) or (rule_for(src, dst, cfg) if src in labels else None)
            return src in failed or src in kept or (r is not None and not r["enabled"] and r["keep_copies"])
        to_delete = [ev for key, ev in existing.items() if key not in desired and not leave_alone(key)] + dupes
        total_copies = len(existing) + len(dupes)
        if to_delete and not force and len(to_delete) > max(10, total_copies // 2):
            log(f"! {name}: would delete {len(to_delete)} of {total_copies} copies; skipped (run with --force if intended)")
            summary["skipped_deletes"] += len(to_delete)
            to_delete = []

        for key, (fields, label, ev) in desired.items():
            have = existing.get(key)
            src = key.split("|", 1)[0]
            shown = '"' + fields["title"] + '"' + (" with details" if fields.get("notes") else "")
            try:
                if have is None:
                    log(f"+ {name}: {fields['title']} {fields['start']}")
                    tell(src, name, ev, True, f"copied as {shown}", "add")
                    if not dry_run:
                        ad.create(fields, key, label)
                    summary["created"] += 1
                    count(src, name, "created")
                elif refresh or not same_copy(have, fields):
                    log(f"~ {name}: {fields['title']} {fields['start']}")
                    tell(src, name, ev, True, f"copied as {shown}; the copy there is out of date", "update")
                    if not dry_run:
                        ad.update(have["uid"], fields, key, label)
                    summary["updated"] += 1
                    count(src, name, "updated")
                else:
                    tell(src, name, ev, True, f"copied as {shown}; already up to date")
            except Exception as e:
                summary["errors"] += 1
                count(src, name, "errors")
                log(f"! {name}: write failed for {key}: {e}")
        for ev in to_delete:
            src = (ev.get("copy_of") or "?|").split("|", 1)[0]
            try:
                log(f"- {name}: {ev['title']} {ev['start']}")
                if decisions is not None:
                    decisions.append({"src": src, "dst": name, "title": ev.get("title") or "", "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"],
                                      "private": False, "copied": False, "action": "remove",
                                      "reason": "its original is gone, or changed so a rule now leaves it out, or its calendar left CalSync"})
                if not dry_run:
                    ad.delete(ev["uid"])
                summary["deleted"] += 1
                count(src, name, "deleted")
            except Exception as e:
                summary["errors"] += 1
                count(src, name, "errors")
                log(f"! {name}: delete failed for {ev['uid']}: {e}")
    for src, ev, why in why_not:
        tell(src, "", ev, False, why)
    return summary


def purge(cals, adapters, dry_run=False, log=print):
    """Delete every copy calsync made on these calendars: the way out if you stop using it."""
    now = now_utc()
    tmin = (now - dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmax = (now + dt.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = {"deleted": 0, "errors": 0}
    for c in cals:
        ad = adapters[c["name"]]
        if getattr(ad, "read_only", False):
            continue
        try:
            for ev in ad.list(tmin, tmax):
                if ev["copy_of"]:
                    if not dry_run:
                        ad.delete(ev["uid"])
                    summary["deleted"] += 1
        except Exception as e:
            summary["errors"] += 1
            log(f"! {c['name']}: {e}")
    return summary


# ---------- OAuth ----------

def _accounts():
    return load_json(os.path.join(SECRETS, "accounts.json"), {})


def _save_account(label, data):
    with file_lock("accounts.lock"):
        a = _accounts()
        a[label] = data
        save_json(os.path.join(SECRETS, "accounts.json"), a)


def remove_account(label):
    with file_lock("accounts.lock"):
        a = _accounts()
        a.pop(label, None)
        save_json(os.path.join(SECRETS, "accounts.json"), a)


def update_config(change):
    """change(cfg) runs with the config locked, so two saves can't lose each other's edit."""
    with file_lock("config.lock"):
        cfg = load_json(CONFIG, {"calendars": []})
        change(cfg)
        save_json(CONFIG, cfg)
        return cfg


def _client(provider):
    c = load_json(os.path.join(SECRETS, f"{provider}-client.json"), None)
    if not c:
        raise SystemExit(f"No {provider} OAuth client yet: run `calsync setup {provider}` first.")
    return c


def google_access_token(label):
    acct, c = _accounts()[label], _client("google")
    r = http("POST", "https://oauth2.googleapis.com/token", form={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": acct["refresh_token"], "grant_type": "refresh_token"})
    return r["access_token"]


def microsoft_access_token(label):
    acct, c = _accounts()[label], _client("microsoft")
    r = http("POST", f"https://login.microsoftonline.com/{acct.get('tenant', 'common')}/oauth2/v2.0/token", form={
        "client_id": c["client_id"], "grant_type": "refresh_token", "refresh_token": acct["refresh_token"],
        "scope": MS_SCOPES})
    if r.get("refresh_token"):  # Microsoft rotates refresh tokens
        acct["refresh_token"] = r["refresh_token"]
        _save_account(label, acct)
    return r["access_token"]


def google_auth_url(client_id, redirect, state=None):
    q = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
         "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent"}
    if state:
        q["state"] = state
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)


def google_finish(label, code, redirect):
    """Trade the code from Google's redirect for a stored sign-in. Returns the problem, or None."""
    c = _client("google")
    r = http("POST", "https://oauth2.googleapis.com/token", form={
        "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    if "refresh_token" not in r:
        return "Google returned no refresh token; remove the app at myaccount.google.com/permissions and try again."
    _save_account(label, {"provider": "google", "refresh_token": r["refresh_token"]})
    return None


def microsoft_device_start(tenant="common"):
    c = _client("microsoft")
    return http("POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
                form={"client_id": c["client_id"], "scope": MS_SCOPES})


def microsoft_device_poll(label, device_code, tenant="common"):
    """True once signed in, False while waiting. Raises if the sign-in failed."""
    c = _client("microsoft")
    try:
        t = http("POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", form={
            "client_id": c["client_id"], "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except RuntimeError as e:
        if "authorization_pending" in str(e) or "slow_down" in str(e):
            return False
        raise
    _save_account(label, {"provider": "outlook", "tenant": tenant, "refresh_token": t["refresh_token"]})
    return True


def account_calendars(label):
    """-> [{id, name, writable}] for a signed-in account. An .ics link is one read-only calendar."""
    acct = _accounts()[label]
    if acct["provider"] == "ics":
        return [{"id": "ics", "name": label, "writable": False}]
    if acct["provider"] == "caldav":
        return caldav_calendars(DavClient(acct["server"], acct["username"], acct["password"]), acct["homes"])
    if acct["provider"] == "google":
        out, page = [], None
        while True:
            q = {"maxResults": "250", **({"pageToken": page} if page else {})}
            r = http("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList?" + urllib.parse.urlencode(q), google_access_token(label))
            out += [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", ""), "writable": c.get("accessRole") in ("owner", "writer")}
                    for c in r.get("items", [])]
            page = r.get("nextPageToken")
            if not page:
                return out
    r = http("GET", "https://graph.microsoft.com/v1.0/me/calendars?$top=100", microsoft_access_token(label))
    return [{"id": c["id"], "name": c.get("name", ""), "writable": bool(c.get("canEdit"))} for c in r.get("value", [])]


def cmd_setup(provider):
    ensure_home()
    if provider == "google":
        cid = input("Google OAuth client ID: ").strip()
        sec = getpass.getpass("Google OAuth client secret (hidden): ").strip()
        save_json(os.path.join(SECRETS, "google-client.json"), {"client_id": cid, "client_secret": sec})
    elif provider == "microsoft":
        cid = input("Microsoft application (client) ID: ").strip()
        save_json(os.path.join(SECRETS, "microsoft-client.json"), {"client_id": cid})
    else:
        raise SystemExit("setup google|microsoft")
    print("Saved.")


def cmd_auth_google(label):
    c = _client("google")
    print("1. Open this link in your browser and sign in as the account for", repr(label) + ":\n\n" + google_auth_url(c["client_id"], GOOGLE_REDIRECT) + "\n")
    print("2. After you click Allow, the browser shows an error page at 127.0.0.1. That's expected.")
    back = input("3. Copy that page's full address from the address bar and paste it here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(back).query).get("code", [back])[0]
    problem = google_finish(label, code, GOOGLE_REDIRECT)
    if problem:
        raise SystemExit(problem)
    print(f"Signed in {label!r}. Its calendars:")
    _print_calendars(label)


def cmd_auth_outlook(label, tenant="common"):
    r = microsoft_device_start(tenant)
    print(r["message"])
    deadline = time.time() + int(r.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(int(r.get("interval", 5)))
        if microsoft_device_poll(label, r["device_code"], tenant):
            print(f"Signed in {label!r}. Its calendars:")
            _print_calendars(label)
            return
    raise SystemExit("Timed out waiting for the Microsoft sign-in.")


def cmd_auth_caldav(label, server=None):
    ensure_home()
    service, preset, note = CALDAV_SERVERS.get(server, ("", None, CALDAV_SERVERS["other"][2]))
    print(note.replace("&amp;", "&"))
    server = preset or (None if server in CALDAV_SERVERS else server) or input("Server address: ").strip()
    username = input("Username: ").strip()
    caldav_connect(label, server, username, getpass.getpass("Password (hidden): "), service)
    _print_calendars(label)


def _print_calendars(label):
    for c in account_calendars(label):
        print(f"  {label}  {c['id']}  ({c['name']}{'' if c['writable'] else ', read-only'})")


# ---------- quick add: one line of text -> an event on the main calendar ----------
# "Dentist tomorrow 3pm", "Gym fri 6-7:30am", "Call Sam 11/3 at 2 for 30m", "Trip nov 14 all day".

_WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
             "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
             "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
_MONTHS = {m: i + 1 for i, ms in enumerate((
    "jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july",
    "aug august", "sep sept september", "oct october", "nov november", "dec december")) for m in ms.split()}
_T = r"(?:noon|midnight|\d{1,2}(?::\d{2})?(?:\s?[ap]m|[ap])?)(?![\w:])"
_D = r"\d+(?:\.\d+)?\s?(?:h|hrs?|hours?)(?:\s?\d+\s?(?:m|mins?|minutes?))?(?!\w)|\d+\s?(?:m|mins?|minutes?)(?!\w)"
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
QUICK_HINT = 'add a day and time, e.g. "fri 3pm"'


def _clock(s):
    """'3pm' -> (3, 0, 'pm'); '15:00' -> (15, 0, None); 'noon' -> (12, 0, 'pm')."""
    s = s.replace(" ", "")
    if re.search(r"\d[AP]$", s):   # "at 4A" is a room
        return None
    s = s.lower()
    if s in ("noon", "midnight"):
        return (12, 0, "pm") if s == "noon" else (12, 0, "am")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?([ap])?m?", s)
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return (h, mi, m.group(3) and m.group(3) + "m") if h <= 23 and mi <= 59 else None


def _h24(h, mer):
    if mer == "am":
        return 0 if h == 12 else h
    if mer == "pm":
        return h if h == 12 else h + 12
    return h if h >= 12 or h == 0 or h >= 8 else h + 12  # bare "at 3" means 3pm, "at 9" 9am


def _strong(s):
    """Whether this alone says it's a time. In capitals only AM, PM, NOON and MIDNIGHT do: 4A is a room, and Noon or Midnight can start a title."""
    return bool(re.search(r"[ap]$|(?i:[ap]m)$|:|noon|midnight|NOON|MIDNIGHT", s.replace(" ", "")))


def quick_parse(text, now=None):
    """-> (event, None) or (None, why). event: title, all_day, start, end (local datetimes, or dates), when."""
    tz = local_tz()
    now = (now or now_utc()).astimezone(tz)
    today = now.date()
    rest = " " + text.strip() + " "
    found = {}

    def take(pattern, fn):
        nonlocal rest
        for m in re.finditer(pattern, rest, re.I):
            if fn(m) is not False:
                rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
                return True
        return False

    def on_date(d):
        found["date"] = d

    def explicit(y, mo, d):
        try:
            day = dt.date(int(y) if y else today.year, mo, int(d))
        except ValueError:
            return False
        if not y and day < today:
            day = day.replace(year=day.year + 1)
        on_date(day)

    def weekday(m):
        d = today + dt.timedelta(days=(_WEEKDAYS[m.group(2).lower()] - today.weekday()) % 7)
        if m.group(1) and m.group(1).lower() == "next" and d == today:
            d += dt.timedelta(days=7)
        found["weekday"] = True
        on_date(d)

    pre = r"(?<!\S)(?:on\s+)?"
    take(r"(?<!\S)all[- ]?day(?!\S)", lambda m: found.update(all_day=True))
    (take(pre + r"(today|tonight|tomorrow|tmrw|tmr)(?!\w)",
          lambda m: on_date(today + dt.timedelta(days=0 if m.group(1).lower() in ("today", "tonight") else 1)))
     or take(pre + r"(\d{4})-(\d{1,2})-(\d{1,2})(?!\w)", lambda m: explicit(m.group(1), int(m.group(2)), m.group(3)))
     or take(pre + r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\w/])",
             lambda m: explicit(m.group(3) and (m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)),
                                int(m.group(1)), m.group(2)))
     or take(pre + rf"({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?!\w)",
             lambda m: explicit(m.group(3), _MONTHS[m.group(1).lower()], m.group(2)))
     or take(pre + rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON})(?:,?\s+(20[2-9]\d))?(?!\w)",
             lambda m: explicit(m.group(3), _MONTHS[m.group(2).lower()], m.group(1)))
     or take(pre + rf"(?:(next|this)\s+)?({_WD})\.?(?!\w)", weekday))

    def time_range(m):
        a, b = m.group(2), m.group(3)
        if not (_strong(a) or _strong(b) or m.group(1)):
            return False
        (h1, m1, x1), (h2, m2, x2) = _clock(a) or (None,) * 3, _clock(b) or (None,) * 3
        if h1 is None or h2 is None:
            return False
        if x2 and not x1:  # "3-4pm", "11-1pm"
            x1 = x2 if _h24(h1, x2) * 60 + m1 < _h24(h2, x2) * 60 + m2 else ("am" if x2 == "pm" else "pm")
        elif x1 and not x2:  # "11am-1"
            x2 = x1 if _h24(h2, x1) * 60 + m2 > _h24(h1, x1) * 60 + m1 else ("am" if x1 == "pm" else "pm")
        found["time"] = (_h24(h1, x1), m1)
        found["until"] = (_h24(h2, x2), m2)

    def duration(m):
        d = re.fullmatch(r"(\d+(?:\.\d+)?)\s?(?:h|hrs?|hours?)(?:\s?(\d+)\s?(?:m|mins?|minutes?))?|(\d+)\s?(?:m|mins?|minutes?)",
                         m.group(1).lower())
        found["minutes"] = int(d.group(3)) if d.group(3) else round(float(d.group(1)) * 60) + int(d.group(2) or 0)

    def single(m):
        if not (_strong(m.group(2)) or m.group(1)):
            return False
        c = _clock(m.group(2))
        if not c or (not c[2] and not m.group(1) and ":" not in m.group(2)):
            return False
        found["time"] = (_h24(c[0], c[2]), c[1])

    take(rf"(?<!\S)(from\s+|at\s+|@\s*)?({_T})\s*(?:-|–|—|to|until|till)\s*({_T})", time_range)
    take(rf"(?<!\S)(?:for\s+)?({_D})", duration)
    "time" in found or take(rf"(?<!\S)(at\s+|@\s*)?({_T})", single)

    title = re.sub(r"\s+", " ", rest).strip()
    for _ in range(3):
        title = re.sub(r"^(?:at|on|for|from|by|@)\s+|\s+(?:at|on|for|from|by|@)$|^[\s,;:–—-]+|[\s,;:–—-]+$", "", title, flags=re.I)
    if not title:
        return None, 'add what it is, e.g. "Dentist fri 3pm"'
    if "time" not in found:
        if "date" not in found:
            return None, QUICK_HINT
        if "minutes" in found and not found.get("all_day"):
            return None, 'add a start time, e.g. "fri 3pm 1h"'
        d = found["date"]
        return {"title": title, "all_day": True, "start": d.isoformat(), "end": (d + dt.timedelta(days=1)).isoformat(),
                "when": f"{d:%a %b} {d.day} (all day)"}, None
    day = found.get("date", today)
    h, mi = found["time"]
    start = dt.datetime(day.year, day.month, day.day, h % 24, mi, tzinfo=tz)
    if "date" not in found and start < now:
        start += dt.timedelta(days=1)  # "3pm" said at 5pm means tomorrow
    elif found.get("weekday") and start < now:
        start += dt.timedelta(days=7)  # "fri 9am" said on Friday at noon means next Friday
    if "until" in found:
        end = start.replace(hour=found["until"][0] % 24, minute=found["until"][1])
        if end <= start:
            end += dt.timedelta(days=1)
    else:
        end = start + dt.timedelta(minutes=found.get("minutes", 60))
    return {"title": title, "all_day": False, "start": start, "end": end, "when": f"{start:%a %b} {start.day}, {_span(start, end)}", "day_stated": "date" in found}, None


def _span(start, end):
    """'3:00–4:00 PM', or '11:00 AM–1:00 PM' when the halves of the day differ."""
    def hm(t):
        return f"{t.hour % 12 or 12}:{t.minute:02d}"
    if (start.hour < 12) == (end.hour < 12) and start.date() == end.date():
        return f"{hm(start)}–{hm(end)} {end:%p}"
    return f"{hm(start)} {start:%p}–{hm(end)} {end:%p}"


def quick_add_note(path, adapter, now=None, log=print):
    """Turn each new top-level '- ' line of the quick-add note (under its "Events" heading, if it has one) into an
    event on adapter. Returns events added. Checkbox lines are left alone, so a task list can share the note."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return 0
    handled, fence = {}, False
    lines = text.split("\n")
    heads = [i for i, l in enumerate(lines) if re.fullmatch(r"#+\s*events\s*", l.strip(), re.I)]
    try:
        for line in lines[heads[0] + 1:] if heads else lines:  # with an "Events" heading, only lines under it
            if line.lstrip().startswith("```"):
                fence = not fence
            m = None if fence else re.match(r"[-*] (?!\[)(.+)", line)
            body = m.group(1).strip() if m else ""
            if not body or line in handled or body.startswith("✅") or "⚠️" in body:
                continue
            ev, why = quick_parse(body, now)
            if ev:
                adapter.create_native(ev)
                handled[line] = f"- ✅ {body} → {ev['when']}"
                log(f"* quick-add: {ev['title']} {ev['when']}")
            else:
                handled[line] = f"{line.rstrip()} ⚠️ {why}"
    finally:
        if handled:
            # Re-read so an edit made while the events were being created is kept; only handled lines change.
            with open(path, encoding="utf-8") as f:
                lines = f.read().split("\n")
            with open(path, "w", encoding="utf-8") as f:  # in place, so a tool syncing the folder sees a normal edit
                f.write("\n".join(handled.get(l, l) for l in lines))
    return sum(v.startswith("- ✅") for v in handled.values())



# ---------- agenda: the calendar written into a folder of Markdown notes ----------
# A calendar that holds a copy of everything gives every calendar in one place.

def agenda_days(events, today, days):
    """events -> {date: ['- 9:00–10:15 AM · Title', ...]} for today..today+days-1, local time."""
    tz = local_tz()
    out = {today + dt.timedelta(days=i): [] for i in range(days)}
    seen = set()
    for ev in sorted(events, key=lambda e: (not e["all_day"], e["start"], e["title"] or "")):
        key = (ev["title"], ev["start"], ev["end"])
        if ev["declined"] or _canceled(ev) or key in seen:
            continue
        seen.add(key)
        title = ev["title"] or "(no title)"
        if ev["all_day"]:
            d, last = dt.date.fromisoformat(ev["start"]), dt.date.fromisoformat(ev["end"])
            while d < last:
                if d in out:
                    out[d].append(f"- All day · {title}")
                d += dt.timedelta(days=1)
        else:
            s = dt.datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).astimezone(tz)
            e = dt.datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).astimezone(tz)
            if s.date() in out:
                out[s.date()].append(f"- {_span(s, e)} · {title}")
    return out


def _write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as f:  # in place, so a tool syncing the folder sees a normal edit
        f.write(text)
    return True


def write_agenda(cfg, adapter, now=None):
    """Write the agenda note, and refresh the Calendar section of existing daily notes. Returns files changed.
    Files are only rewritten when an event changed, so devices aren't sent a new copy every run."""
    tz = local_tz()
    days = cfg.get("days", 14)
    today = (now or now_utc()).astimezone(tz).date()
    start = dt.datetime(today.year, today.month, today.day, tzinfo=tz).astimezone(dt.timezone.utc)
    events = list_for_people(adapter, start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             (start + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    by_day = agenda_days(events, today, days)
    vault, changed = cfg["vault"], 0

    lines = ["# Calendar", "",
             f"Every calendar in one list, for the next {days} days. calsync keeps it current.", ""]
    for d, items in by_day.items():
        if not items and d > today + dt.timedelta(days=1):
            continue
        label = {0: "Today · ", 1: "Tomorrow · "}.get((d - today).days, "")
        lines += [f"## {label}{d:%A, %B} {d.day}", *(items or ["- Nothing scheduled"]), ""]
    changed += _write_if_changed(os.path.join(vault, cfg.get("note", "Calendar.md")), "\n".join(lines))

    folder = os.path.join(vault, cfg.get("daily_folder", "Daily"))
    for d, items in by_day.items():
        path = os.path.join(folder, f"{d.isoformat()}.md")
        try:
            with open(path, encoding="utf-8") as f:
                note = f.read().split("\n")
        except FileNotFoundError:
            continue
        heads = [i for i, l in enumerate(note) if re.fullmatch(r"##\s.*\bcalendar\s*", l.strip(), re.I)]
        if not heads:
            continue
        i = heads[0] + 1
        j = next((k for k in range(i, len(note)) if note[k].startswith("#")), len(note))
        note[i:j] = [*(items or ["- Nothing on the calendar"]), ""]
        changed += _write_if_changed(path, "\n".join(note))
    return changed


# ---------- .ics export: real events only, one file per source calendar ----------

EXPORT_PAST_DAYS, EXPORT_FUTURE_DAYS = 30, 365


def _ics_escape(text):
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", "", text)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line):
    """RFC 5545: lines of at most 75 octets, never splitting a UTF-8 character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    return out + [cur]


def _ics_when(key, value, all_day):
    if all_day:
        return f"{key};VALUE=DATE:{value.replace('-', '')}"
    return f"{key}:{value.replace('-', '').replace(':', '')}"


def _vevents(text):
    """The components inside a VCALENDAR (VEVENTs, VTIMEZONEs...) as lists of unfolded lines, BEGIN and END included."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n ", "").replace("\n\t", "").split("\n")
    out, cur, depth = [], None, 0
    for line in lines:
        if line.startswith("BEGIN:") and line != "BEGIN:VCALENDAR":
            if depth == 0:
                cur = []
            depth += 1
        if cur is not None:
            cur.append(line)
        if line.startswith("END:") and line != "END:VCALENDAR" and depth:
            depth -= 1
            if depth == 0:
                out.append(cur)
                cur = None
    return out


def _master(blocks):
    return next(b for b in blocks if b[0] == "BEGIN:VEVENT" and not _prop(b, "RECURRENCE-ID"))


def _prop(block, name):
    """The first top-level line of this property in a component, or None."""
    depth = 0
    for line in block[1:-1]:
        depth += line.startswith("BEGIN:") - line.startswith("END:")
        if depth == 0 and re.match(re.escape(name) + r"[;:]", line):
            return line
    return None


def _param_value(line):
    head, value = line.split(":", 1)
    params = dict(p.split("=", 1) for p in head.split(";")[1:] if "=" in p)
    return {k.upper(): v.strip('"') for k, v in params.items()}, value


def _ics_time_text(v, all_day):
    if all_day:
        return _day(v)
    return _aware(v).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _vevent_edit(block, ch):
    """A VEVENT with the Calendar tab's changes applied: title, start/end, all_day, location, description, busy, private, colour, repeat."""
    sets = {}
    if "title" in ch:
        sets["SUMMARY"] = "SUMMARY:" + _ics_escape(ch["title"])
    if "start" in ch:
        sets["DTSTART"] = _ics_when("DTSTART", _ics_time_text(ch["start"], ch.get("all_day")), ch.get("all_day"))
        sets["DTEND"] = _ics_when("DTEND", _ics_time_text(ch["end"], ch.get("all_day")), ch.get("all_day"))
        sets["DURATION"] = None
    if "location" in ch:
        sets["LOCATION"] = "LOCATION:" + _ics_escape(ch["location"]) if ch["location"] else None
    if "description" in ch:
        sets["DESCRIPTION"] = "DESCRIPTION:" + _ics_escape(ch["description"]) if ch["description"] else None
    if "busy" in ch:
        sets["TRANSP"] = "TRANSP:" + ("OPAQUE" if ch["busy"] else "TRANSPARENT")
    if "private" in ch:
        sets["CLASS"] = "CLASS:" + ("PRIVATE" if ch["private"] else "PUBLIC")
    if "colour" in ch:
        sets["COLOR"] = f"COLOR:{ch['colour']}" if ch["colour"] in PALETTE else None
    if "repeat" in ch:
        sets["RRULE"] = f"RRULE:{ch['repeat']}" if ch["repeat"] else None
    out, depth, done = [block[0]], 0, set()
    for line in block[1:-1]:
        name = re.match(r"[A-Za-z-]+", line)
        name = name.group(0).upper() if name else ""
        depth += line.startswith("BEGIN:") - line.startswith("END:")
        if depth == 0 and name in sets:
            if sets[name] and name not in done:
                out.append(sets[name])
            done.add(name)
            continue
        out.append(line)
    out += [v for k, v in sets.items() if v and k not in done]
    return out + [block[-1]]


def _vevent_lines(ev):
    """The lines of a new VEVENT (UID and DTSTAMP are added by the writer)."""
    block = _vevent_edit(["BEGIN:VEVENT", "END:VEVENT"], {k: ev[k] for k in ("title", "start", "end", "all_day", "location", "description", "busy", "private", "colour", "repeat") if k in ev})
    return block[1:-1]


def ics_file(name, label, events, stamp):
    """A calendar of expanded instances, each with its own UID. Same hiding as a copy: a private event says Busy.
    A location holding a link is left out, since join links can carry passcodes."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//calsync//export//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:" + _ics_escape(label)]
    for ev in events:
        f = render(ev, name, {"name": ""}, rule_for(name, "", {}))
        uid = hashlib.sha256(f"{name}|{ev['uid']}".encode()).hexdigest()[:32]
        lines += ["BEGIN:VEVENT", f"UID:{uid}@calsync", "DTSTAMP:" + stamp,
                  _ics_when("DTSTART", f["start"], f["all_day"]), _ics_when("DTEND", f["end"], f["all_day"]),
                  "SUMMARY:" + _ics_escape(f["title"]), "TRANSP:" + ("OPAQUE" if f["busy"] else "TRANSPARENT")]
        if f["location"] and "://" not in f["location"]:
            lines.append("LOCATION:" + _ics_escape(f["location"]))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(part + "\r\n" for line in lines for part in _ics_fold(line))


def export_calendars(cfg, cals, adapters, now=None):
    """Write <label>.ics for each calendar in cfg["calendars"]. Returns label -> events written.
    The order there decides which file keeps a meeting that sits on several accounts. If any calendar can't be
    listed, nothing is written: the last good set is kept, so a meeting never lands in two files."""
    now = now or now_utc()
    tmin = (now - dt.timedelta(days=EXPORT_PAST_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmax = (now + dt.timedelta(days=EXPORT_FUTURE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_name = {c["name"]: c for c in cals}
    seen, files = set(), []
    for name in cfg["calendars"]:
        keep = []
        for ev in sorted(list_for_people(adapters[name], tmin, tmax), key=lambda e: (e["start"], e["uid"])):
            if ev["copy_of"] or ev["declined"] or _canceled(ev) or ev.get("kind") == "birthday":  # the same rule sync uses for a source event
                continue
            key = (ev.get("ical") or f"{name}|{ev['uid']}", ev["start"])
            if key not in seen:
                seen.add(key)
                keep.append(ev)
        files.append((name, by_name[name].get("label") or name, keep))
    os.makedirs(cfg["folder"], mode=0o700, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for name, label, keep in files:
        path = os.path.join(cfg["folder"], re.sub(r"[^\w .-]", "_", label).lstrip(".") + ".ics")
        tmp = path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8", newline="") as f:
            f.write(ics_file(name, label, keep, stamp))
        os.replace(tmp, path)
    return {label: len(keep) for _, label, keep in files}


# ---------- CLI ----------

ACTIVITY_KEEP = 300      # runs kept in the activity log


def record_activity(entry):
    """Add one run to the activity log (activity.jsonl in the settings folder, private to this machine)."""
    with file_lock("activity.lock"):
        try:
            with open(ACTIVITY, encoding="utf-8") as f:
                rows = f.read().splitlines()[-(ACTIVITY_KEEP - 1):]
        except FileNotFoundError:
            rows = []
        rows.append(json.dumps(entry))
        tmp = ACTIVITY + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        os.replace(tmp, ACTIVITY)


def activity(limit=50, pair=None):
    """The latest runs, newest first. pair="SRC>DST" keeps only runs that changed that direction, and only its lines."""
    try:
        with open(ACTIVITY, encoding="utf-8") as f:
            rows = [json.loads(r) for r in f.read().splitlines() if r.strip()]
    except (FileNotFoundError, ValueError):
        return []
    out = []
    for r in reversed(rows):
        if pair:
            if pair not in r.get("pairs", {}) and not r.get("pair") == pair:
                continue
            src, dst = pair.split(">", 1)
            r = dict(r, lines=[l for l in r.get("lines", []) if l[2:].startswith(dst + ":")])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def pair_status(cfg=None, last=None, running=False):
    """"SRC>DST" -> ("synced" | "syncing" | "error" | "off" | "waiting", a few words), from the last run."""
    cfg = cfg or load_json(CONFIG, {"calendars": []})
    last = last if last is not None else load_json(LASTRUN, None)
    cals = cfg["calendars"]
    out = {}
    for d in cals:
        for s in cals:
            if s["name"] == d["name"]:
                continue
            key = f"{s['name']}>{d['name']}"
            rule = rule_for(s["name"], d["name"], cfg)
            writable = not d.get("read_only")
            p = (last or {}).get("pairs", {}).get(key, {})
            if not writable or not rule["enabled"]:
                out[key] = ("off", "off" if writable else f"{d.get('label') or d['name']} is never written to")
            elif running:
                out[key] = ("syncing", "syncing now")
            elif not last or not cfg.get("enabled"):
                out[key] = ("waiting", "not started yet")
            elif s["name"] in last.get("failed", []) or d["name"] in last.get("failed", []) or p.get("errors"):
                out[key] = ("error", "had a problem at the last run")
            else:
                out[key] = ("synced", "synced")
    return out


def run_once(dry_run=False, force=False, log=print, decisions=None, pair=None, refresh=False, trigger="timer"):
    """One whole pass. Returns the summary, or None if a run is already going. Problems land in summary["problems"].
    decisions: see sync(). pair and refresh: a Resync of one direction. Real runs are written to the activity log."""
    with file_lock("run.lock", blocking=False) as free:  # one run at a time: the timer, `calsync new` and the web page can overlap
        if not free:
            return None
        cfg = load_json(CONFIG, {"calendars": []})
        cals = cfg["calendars"]
        started = now_utc()
        problems, changes = [], []

        def note(line):
            if line.startswith("!"):
                problems.append(line)
            elif line[:2] in ("+ ", "~ ", "- "):
                changes.append(line)
            log(line)

        if len(cals) < 2:
            s = {"created": 0, "updated": 0, "deleted": 0, "skipped_deletes": 0, "failed": [], "errors": 0}
            note("Fewer than two calendars configured; nothing to sync.")
        else:
            adapters = {c["name"]: make_adapter(c) for c in cals}
            added, qa_errors, qa = 0, 0, cfg.get("quick_add")
            if qa and not dry_run and not pair:  # before the sync, so new events are copied everywhere in this same run
                try:
                    added = quick_add_note(qa["note"], adapters[qa["calendar"]], log=note)
                except Exception as e:
                    qa_errors = 1
                    note(f"! quick-add: {e}")
            s = sync(cals, adapters, dry_run=dry_run, force=force, log=note, cfg=cfg, decisions=decisions, pair=pair, refresh=refresh)
            s["errors"] += qa_errors
            s["quick_added"] = added
            ag = cfg.get("agenda")
            if ag and not dry_run and not pair:  # after the sync, so it shows the copies this run just made
                try:
                    s["agenda_files"] = write_agenda(ag, adapters[ag["calendar"]])
                except Exception as e:
                    s["errors"] += 1
                    note(f"! agenda: {e}")
            ex = cfg.get("export")
            if ex and not dry_run and not pair:
                try:
                    s["exported"] = export_calendars(ex, cals, adapters)
                except Exception as e:
                    s["errors"] += 1
                    note(f"! export: {e}")
        s.update(at=started.strftime("%Y-%m-%dT%H:%M:%SZ"), dry_run=dry_run, seconds=round((now_utc() - started).total_seconds(), 1), problems=problems[:20])
        if not dry_run:
            if pair:        # a Resync of one direction updates that pair's line in the last run, not the whole run
                last = load_json(LASTRUN, None) or {}
                last.setdefault("pairs", {})[pair] = s.get("pairs", {}).get(pair, {"created": 0, "updated": 0, "deleted": 0, "errors": 0})
                save_json(LASTRUN, last if last.get("at") else s)
            else:
                save_json(LASTRUN, s)
            try:
                record_activity({k: s.get(k) for k in ("at", "created", "updated", "deleted", "errors", "skipped_deletes", "failed", "pairs", "seconds", "problems")}
                                | {"trigger": trigger, "pair": pair, "lines": changes[:200]})
            except OSError:
                pass
        return s


def cmd_run(dry_run, force):
    s = run_once(dry_run, force)
    if s is None:
        print("Another run is going; try again in a moment.")
        return 0
    print(json.dumps(s))
    return 1 if s["errors"] or s["failed"] else 0


# ---------- quick add with guests, a reminder and an overlap check; skipping one occurrence ----------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_REMIND = re.compile(r"(?<!\S),?\s*(?:and\s+)?remind(?:er)?(?:\s+me)?\s+(\d+(?:\.\d+)?)\s?(m|mins?|minutes?|h|hrs?|hours?|d|days?)(?:\s+(?:before|ahead|early))?(?!\w)", re.I)
MAX_GUESTS = 20


def quick_plan(text, now=None):
    """quick_parse, plus who to invite (email addresses) and a reminder ("remind me 1h before").
    Names without an address stay in the title: "Breakfast with Sam". -> (event, None) or (None, why)."""
    guests = []

    def grab(m):
        guests.append(m.group(0).lower())
        return " "
    rest = _EMAIL.sub(grab, text or "")
    remind = None
    m = _REMIND.search(rest)
    if m:
        n, unit = float(m.group(1)), m.group(2).lower()
        remind = int(n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1))
        rest = rest[:m.start()] + " " + rest[m.end():]
    if remind is not None and not 0 <= remind <= 40320:
        return None, "a reminder can be up to 4 weeks before"
    if len(set(guests)) > MAX_GUESTS:
        return None, f"at most {MAX_GUESTS} guests"
    ev, why = quick_parse(rest, now)
    if not ev:
        return None, why
    title = re.sub(r"\s+(?:and|,)\s+(?=(?:and|,|with)\b|$)", " ", ev["title"])       # "with Sam and  " left by a removed address
    title = re.sub(r"(?:\s*(?:,|\band\b|\bwith\b))+\s*$", "", title).strip() or ev["title"]
    ev.update(title=title, guests=sorted(set(guests)), remind_minutes=remind)
    return ev, None


def _when_utc(v, end=False):
    """A quick-add start or end as an aware UTC datetime; an all-day date counts from local midnight."""
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc)
    d = v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])
    return dt.datetime.combine(d, dt.time(), local_tz()).astimezone(dt.timezone.utc)


def busy_overlaps(ev, cals=None):
    """The real, busy events on any calendar that a new event would sit on top of. -> (overlaps, calendars not read).
    Copies calsync made are left out (their source is checked instead), and so are all-day items and free time."""
    if ev.get("all_day"):
        return [], []
    cfg = load_json(CONFIG, {"calendars": []})
    cals = cals if cals is not None else cfg["calendars"]
    lo, hi = _when_utc(ev["start"]), _when_utc(ev["end"], True)
    tmin, tmax = (lo - dt.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"), (hi + dt.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found, seen, unread, tz = [], set(), [], local_tz()
    for c in cals:
        try:
            evs = list_for_people(make_adapter(c), tmin, tmax)
        except Exception:
            unread.append(c.get("label") or c["name"])
            continue
        for x in evs:
            try:                                # anyone can invite you, so one strange event mustn't stop the check
                if x["copy_of"] or x["declined"] or _canceled(x) or x["all_day"] or not x["busy"]:
                    continue
                s = dt.datetime.strptime(x["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
                f = dt.datetime.strptime(x["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
                key = (str(x.get("ical") or x["uid"]), x["start"])
                if not (s < hi and lo < f) or key in seen:
                    continue
                s, f = s.astimezone(tz), f.astimezone(tz)
                row = {"title": "Busy" if x["private"] else (str(x["title"] or "") or "(no title)"), "calendar": c.get("label") or c["name"],
                       "start": s, "end": f, "when": _span(s, f)}
            except Exception:
                continue
            seen.add(key)
            found.append(row)
    return sorted(found, key=lambda o: o["start"]), unread


def writable_calendars(cfg=None):
    cfg = cfg or load_json(CONFIG, {"calendars": []})
    accounts = _accounts()
    return [c for c in cfg["calendars"] if not c.get("read_only") and not c.get("cannot_write")
            and accounts.get(c.get("account"), {}).get("provider") not in (None, "ics")]


def quick_add(text, calendar=None, confirm_overlap=False, confirm_guests=False, sync=True, now=None):
    """One quick add, the same for the web page, the agent tools and the command line.
    Returns {"status": "invalid" | "confirm" | "added", ...}. "confirm" lists what needs a yes first: overlaps
    (book anyway?) and guests (send invites?). Nothing is written until both are confirmed. After adding, it syncs
    right away so a booking page reading any synced calendar sees the time as busy within seconds."""
    ev, why = quick_plan(text, now)
    if not ev:
        return {"status": "invalid", "why": why}
    cfg = load_json(CONFIG, {"calendars": []})
    writable = writable_calendars(cfg)
    cal = next((c for c in writable if c["name"] == calendar), None) if calendar else None
    if calendar and not cal:
        return {"status": "invalid", "why": f"{calendar} isn't a calendar calsync can add to"}
    try:
        cal = cal or main_calendar(cfg)
    except SystemExit as e:
        return {"status": "invalid", "why": str(e)}
    adapter = make_adapter(cal)
    if ev["guests"] and not isinstance(adapter, GoogleCalendar):
        return {"status": "invalid", "why": "invites go out from a Google calendar: pick one to add it to"}
    overlaps, unread = busy_overlaps(ev)
    out = {"event": ev, "calendar": cal.get("label") or cal["name"], "overlaps": overlaps, "unread": unread}
    needs = [n for n, ok in (("overlap", confirm_overlap or not overlaps), ("guests", confirm_guests or not ev["guests"])) if not ok]
    if needs:
        return dict(out, status="confirm", needs=needs)
    adapter.create_native(ev)
    out.update(status="added", synced=False)
    if sync and cfg.get("enabled"):
        started = time.time()
        summary = run_once(log=lambda *_: None)
        while summary is None and time.time() - started < 45:     # the timer's own sync was mid-run: wait for it, then sync this in
            time.sleep(2)
            summary = run_once(log=lambda *_: None)
        if summary is not None:
            out.update(synced=not summary.get("errors"), sync_seconds=round(time.time() - started, 1))
    return out


def skipped(cfg=None):
    """Occurrences the person said not to copy ("Skip this one"), as copy keys "SOURCE|UID". Old ones drop off."""
    cfg = cfg or load_json(CONFIG, {})
    today = now_utc().date().isoformat()
    return {s["key"] for s in cfg.get("skips", []) if s.get("until", "9999") >= today}


def skip_occurrence(key, title, start):
    """Stop copying one occurrence; its copies go at the next sync. The source calendar is left alone."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}\|.{1,1000}", key or ""):
        raise ValueError("not an event calsync knows")
    until = (_when_utc(start).date() + dt.timedelta(days=2)).isoformat()
    def change(c):
        keep = [s for s in c.get("skips", []) if s["key"] != key and s.get("until", "9999") >= now_utc().date().isoformat()]
        c["skips"] = keep + [{"key": key, "title": str(title)[:200], "start": str(start)[:25], "until": until}]
    update_config(change)


def unskip_occurrence(key):
    update_config(lambda c: c.update(skips=[s for s in c.get("skips", []) if s["key"] != key]))


def main_calendar(cfg):
    """Where quick add puts events: add_to (set in Settings), then quick_add.calendar, else the first calendar calsync can write to."""
    cals, want = cfg["calendars"], cfg.get("add_to") or cfg.get("quick_add", {}).get("calendar")
    accounts = _accounts()
    for c in cals:
        writable = not c.get("read_only") and accounts.get(c.get("account"), {}).get("provider") != "ics"
        if c["name"] == want or (not want and writable):
            return c
    raise SystemExit("No calendar to add to yet: add one with `calsync add`.")


# ---------- the Calendar tab's own events: create, edit, move, delete, answer; search ----------
# These write real events, not copies. The next sync copies them out through the rules, like any other event.

REPEATS = {"": "Doesn't repeat", "daily": "Every day", "weekdays": "Every weekday", "weekly": "Every week",
           "monthly": "Every month", "yearly": "Every year"}
SCOPES = ("this", "following", "all")
MAX_TEXT = 4000


def repeat_rule(preset, start, until=None, count=None):
    """A repeat choice from the editor as an RRULE (without "RRULE:"), or "" for none."""
    if not preset:
        return ""
    local = _aware(start).astimezone(local_tz())
    rule = {"daily": "FREQ=DAILY", "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "weekly": f"FREQ=WEEKLY;BYDAY={DAYS[local.weekday()][:2].upper()}",
            "monthly": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY"}[preset]
    if until:
        rule += ";UNTIL=" + _aware(until).replace(hour=23, minute=59).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    elif count:
        rule += f";COUNT={max(1, min(int(count), 730))}"
    return rule


def calendar_named(name, cfg=None):
    cfg = cfg or load_json(CONFIG, {"calendars": []})
    c = next((c for c in cfg["calendars"] if c["name"] == name), None)
    if not c:
        raise ValueError(f"{name} isn't one of your calendars")
    return c


def _editable(name, cfg=None):
    c = calendar_named(name, cfg)
    if c.get("read_only") or c.get("cannot_write"):
        raise ValueError(f"{c.get('label') or name} is read only in CalSync, so edit this event where it lives")
    ad = make_adapter(c)
    if not hasattr(ad, "create_event"):
        raise ValueError(f"{c.get('label') or name} can't be edited from CalSync")
    return ad


def clean_event(raw, partial=False):
    """Fields from the editor or an agent, checked: title, start, end, all_day, location, description, busy,
    private, colour, guests, conference, repeat, remind_minutes. partial keeps only the keys given."""
    out = {}
    text = lambda v, n: re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(v or ""))[:n]
    if "title" in raw or not partial:
        out["title"] = text(raw.get("title"), 300).strip() or "(no title)"
    if "start" in raw or not partial:
        all_day = bool(raw.get("all_day"))
        start, end = _aware(raw["start"]), _aware(raw.get("end") or raw["start"])
        if all_day:
            start, end = start.date(), max(end.date(), start.date())
            end = end + dt.timedelta(days=1) if end == start or raw.get("end_inclusive") else end
        elif end <= start:
            end = start + dt.timedelta(hours=1)
        if (end - start) > dt.timedelta(days=60):
            raise ValueError("an event can be at most 60 days long")
        out.update(start=start, end=end, all_day=all_day)
    for k, n in (("location", 500), ("description", MAX_TEXT)):
        if k in raw:
            out[k] = text(raw[k], n).strip()
    for k in ("busy", "private"):
        if k in raw:
            out[k] = bool(raw[k])
    if "colour" in raw:
        out["colour"] = raw["colour"] if raw["colour"] in PALETTE else ""
    if "guests" in raw:
        guests = raw["guests"] if isinstance(raw["guests"], list) else _EMAIL.findall(str(raw["guests"] or ""))
        guests = sorted({g.strip().lower() for g in guests if _EMAIL.fullmatch(g.strip())})
        if len(guests) > MAX_GUESTS:
            raise ValueError(f"at most {MAX_GUESTS} guests")
        out["guests"] = guests
    if raw.get("conference") in ("meet", "teams") or str(raw.get("conference") or "").startswith("https://"):
        out["conference"] = str(raw["conference"])[:500]
    if "repeat" in raw:
        rule = str(raw["repeat"] or "")
        if rule and not re.fullmatch(r"FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;(INTERVAL=\d{1,3}|BYDAY=[A-Z,]{2,20}|UNTIL=\d{8}T\d{6}Z|COUNT=\d{1,3}))*", rule):
            raise ValueError("that repeat rule isn't one CalSync can write")
        out["repeat"] = rule
    if raw.get("remind_minutes") not in (None, ""):
        out["remind_minutes"] = max(0, min(int(raw["remind_minutes"]), 40320))
    return out


def event_create(cal_name, raw, notify=True):
    ad = _editable(cal_name)
    ev = clean_event(raw)
    if ev.get("guests") and not isinstance(ad, (GoogleCalendar, OutlookCalendar)):
        raise ValueError("invites go out from a Google or Microsoft calendar")
    if ev.get("conference") == "meet" and not isinstance(ad, GoogleCalendar):
        raise ValueError("a Meet link comes from a Google calendar")
    if ev.get("conference") == "teams" and not isinstance(ad, OutlookCalendar):
        raise ValueError("a Teams link comes from a Microsoft calendar")
    return ad.create_event(ev, notify=notify)


def event_update(cal_name, uid, raw, scope="this", notify=True, href=None):
    ad = _editable(cal_name)
    ch = clean_event(raw, partial=True)
    scope = scope if scope in SCOPES else "this"
    if isinstance(ad, CalDavCalendar):
        return ad.update_event(uid, ch, scope, notify, href=href)
    return ad.update_event(uid, ch, scope, notify)


def event_delete(cal_name, uid, scope="this", notify=True, href=None):
    ad = _editable(cal_name)
    scope = scope if scope in SCOPES else "this"
    if isinstance(ad, CalDavCalendar):
        return ad.delete_event(uid, scope, notify, href=href)
    return ad.delete_event(uid, scope, notify)


def event_respond(cal_name, uid, answer):
    if answer not in ("yes", "maybe", "no"):
        raise ValueError("answer yes, maybe or no")
    ad = make_adapter(calendar_named(cal_name))
    if not hasattr(ad, "respond"):
        raise ValueError("answer this invite where it was sent: this calendar can't answer from CalSync")
    ad.respond(uid, answer)


def original_of(copy_of, cfg=None):
    """A copy's original: (calendar config, event uid), or None if its calendar is gone. Editing a copy edits this."""
    src, _, uid = (copy_of or "").partition("|")
    cfg = cfg or load_json(CONFIG, {"calendars": []})
    c = next((c for c in cfg["calendars"] if c["name"] == src), None)
    return (c, uid) if c and uid else None


def parse_search(text):
    """'dentist cal:work from:sam@x.com before:2026-12-01 after:2026-11-01' -> {"words", "cal", "from", "before", "after"}."""
    out = {"words": [], "cal": "", "from": "", "before": None, "after": None}
    for tok in (text or "").split():
        k, _, v = tok.partition(":")
        k = k.lower()
        if k in ("cal", "from") and v:
            out[k] = v.lower()
        elif k in ("before", "after") and v:
            try:
                out[k] = dt.date.fromisoformat(v)
            except ValueError:
                out["words"].append(tok.lower())
        else:
            out["words"].append(tok.lower())
    return out


def search_events(text, cfg=None, now=None, limit=200):
    """Real events on every calendar matching text (title, place, notes, guests), soonest first. -> (rows, problems)."""
    cfg = cfg or load_json(CONFIG, {"calendars": []})
    q = parse_search(text)
    today = (now or now_utc()).astimezone(local_tz()).date()
    lo = q["after"] or today - dt.timedelta(days=90)
    hi = q["before"] or today + dt.timedelta(days=365)
    z = lambda d: dt.datetime.combine(d, dt.time(), local_tz()).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, problems, seen = [], [], set()
    for c in cfg["calendars"]:
        label = c.get("label") or c["name"]
        if q["cal"] and q["cal"] not in (c["name"].lower(), label.lower()):
            continue
        try:
            evs = list_for_people(make_adapter(c), z(lo), z(hi))
        except Exception as e:
            problems.append(f"{label}: {str(e)[:120]}")
            continue
        for ev in evs:
            if ev.get("copy_of") or ev.get("declined") or _canceled(ev):
                continue
            hay = " ".join([ev.get("title") or "", ev.get("location") or "", ev.get("description") or "", " ".join(ev.get("attendees") or [])]).lower()
            if any(w not in hay for w in q["words"]):
                continue
            if q["from"] and q["from"] not in (ev.get("organizer") or "").lower():
                continue
            key = (ev.get("ical") or ev["uid"], ev["start"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(ev, calendar=c["name"]))
    rows.sort(key=lambda e: e["start"])
    return rows[:limit], problems


def changed_since(cfg, since):
    """The calendars that changed after since (UTC 'Z'), from one small request each. Only Google and Microsoft can say
    cheaply; CalDAV and .ics links wait for the normal interval."""
    out = []
    for c in cfg["calendars"]:
        try:
            ad = make_adapter(c)
            if isinstance(ad, (GoogleCalendar, OutlookCalendar)) and ad.changed_since(since):
                out.append(c["name"])
        except Exception:
            continue
    return out


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "setup" and rest:
        cmd_setup(rest[0])
    elif cmd == "web":
        import calsync_web
        return calsync_web.serve()
    elif cmd == "mcp":
        import calsync_mcp
        return calsync_mcp.main(rest)
    elif cmd == "reset-password":
        import calsync_web
        return calsync_web.reset_password_here(rest)
    elif cmd == "auth" and len(rest) >= 2 and rest[0] == "google":
        cmd_auth_google(rest[1])
    elif cmd == "auth" and len(rest) >= 2 and rest[0] == "outlook":
        cmd_auth_outlook(rest[1], rest[2] if len(rest) > 2 else "common")
    elif cmd == "auth" and len(rest) >= 2 and rest[0] == "caldav":
        cmd_auth_caldav(rest[1], rest[2] if len(rest) > 2 else None)
    elif cmd == "auth" and len(rest) >= 2 and rest[0] == "ics":
        url = getpass.getpass("Published calendar link (.ics, hidden): ").strip()
        IcsCalendar(url).list(now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), (now_utc() + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        url = clean_ics_url(url)
        _save_account(rest[1], {"provider": "ics", "url": url})
        print(f"Saved {rest[1]!r}. Add it with: calsync add {rest[1]} {rest[1]} ics")
    elif cmd == "calendars":
        for label, a in _accounts().items():
            if a["provider"] == "ics":
                print(f"  {label}  ics  (published link, read-only)")
            else:
                _print_calendars(label)
    elif cmd == "add" and len(rest) >= 3:
        if rest[1] not in _accounts():
            raise SystemExit(f"account {rest[1]!r} is not signed in")
        if not NAME_OK.fullmatch(rest[0]):
            raise SystemExit("A calendar name uses letters, numbers, - and _ (up to 40).")
        only = [rest[i + 1] for i, x in enumerate(rest[:-1]) if x == "--only"]
        update_config(lambda cfg: cfg.update(calendars=[c for c in cfg["calendars"] if c["name"] != rest[0]] + [{
            "name": rest[0], "label": rest[0], "account": rest[1], "calendar_id": rest[2],
            "busy_only": "--busy-only" in rest, "read_only": "--read-only" in rest,
            "free": "--free" in rest, "only": only[0] if only else None,
            "skip_sources": [rest[i + 1] for i, x in enumerate(rest[:-1]) if x == "--skip"]}]))
        print(f"Added {rest[0]}.")
    elif cmd == "remove" and rest:
        update_config(lambda cfg: cfg.update(calendars=[c for c in cfg["calendars"] if c["name"] != rest[0]]))
        print(f"Removed {rest[0]} (its copies elsewhere are deleted on the next run; copies on it stay until you remove them).")
    elif cmd == "run":
        return cmd_run("--dry-run" in rest, "--force" in rest)
    elif cmd == "purge":
        cals = load_json(CONFIG, {"calendars": []})["calendars"]
        names = [x for x in rest if not x.startswith("--")]
        cals = [c for c in cals if not names or c["name"] in names]
        s = purge(cals, {c["name"]: make_adapter(c) for c in cals}, "--dry-run" in rest)
        print(json.dumps(s))
        return 1 if s["errors"] else 0
    elif cmd == "new" and rest:
        cfg = load_json(CONFIG, {"calendars": []})
        ev, why = quick_parse(" ".join(x for x in rest if x != "--check"))
        if not ev:
            raise SystemExit(f"Not added: {why}")
        cal = main_calendar(cfg)
        print(f"{ev['title']}: {ev['when']} on {cal['name']}")
        if "--check" in rest:
            return 0
        print("id:", make_adapter(cal).create_native(ev))
        return cmd_run(False, False)  # copy it everywhere now instead of at the next timer run
    elif cmd == "status":
        for c in load_json(CONFIG, {"calendars": []})["calendars"]:
            flags = ", ".join([f for f in ("busy_only", "read_only", "free") if c.get(f)] + [f"skips {x}" for x in c.get("skip_sources", [])])
            print(f"  {c['name']}: {c['account']} / {c['calendar_id']}" + (f" ({flags})" if flags else ""))
        print("last run:", json.dumps(load_json(LASTRUN, None)))
    else:
        print(__doc__.strip())
        return 2
    return 0


def cli():
    for stream in (sys.stdout, sys.stderr):  # a Windows console can't print every event title
        stream.reconfigure(errors="replace")
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
