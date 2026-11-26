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

HOME = os.environ.get("CALSYNC_HOME") or os.path.expanduser("~/.calsync")
SECRETS = os.path.join(HOME, "secrets")
CONFIG = os.path.join(HOME, "config.json")
LASTRUN = os.path.join(HOME, "last-run.json")
TAG = "calsync"                      # Google private extended property key
ICS_TAG = "X-CALSYNC"                # the same tag on a CalDAV event
COPY_UID = "calsync-"                # every CalDAV copy's UID starts with this, so a copy is known even if a server drops ICS_TAG
COPY_NOTE = re.compile(r"Synced from \S{1,60} by calsync\. Edit the original there")   # the note every copy carries: a published
#   feed keeps it even where it drops the tag (Google's secret address does), so a copy read back through a link is still known
MS_PROP = "String {6f8f3c3e-7a51-4d0b-9a8e-2c1f5b7d9e44} Name calsync"  # Graph extended property id
PAST_DAYS, FUTURE_DAYS = 0, 120  # events that already ended are neither copied nor cleaned up
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

def render(ev, src_name, dst_cfg):
    """What the copy of ev on dst should look like (comparable fields only)."""
    hide = dst_cfg.get("busy_only") or ev["private"]
    return {
        "title": "Busy" if hide else (ev["title"] or "(no title)"),
        "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"],
        "busy": ev["busy"],
        "location": "" if hide else (ev["location"] or ""),
    }


def comparable(ev):
    return {"title": ev.get("title") or "", "start": ev["start"], "end": ev["end"],
            "all_day": ev["all_day"], "busy": ev["busy"], "location": ev.get("location") or ""}


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
        if it.get("status") == "cancelled" or it.get("eventType") in ("workingLocation", "birthday"):
            return None
        s, e = it.get("start", {}), it.get("end", {})
        all_day = "date" in s
        declined = any(a.get("self") and a.get("responseStatus") == "declined" for a in it.get("attendees", []))
        return {
            "uid": it["id"], "ical": it.get("iCalUID"), "title": it.get("summary", ""),
            "start": s["date"] if all_day else to_utc_z(s["dateTime"]),
            "end": e["date"] if all_day else to_utc_z(e["dateTime"]),
            "all_day": all_day, "busy": it.get("transparency") != "transparent",
            "location": it.get("location", ""), "private": it.get("visibility") in ("private", "confidential"),
            "declined": declined,
            "copy_of": it.get("extendedProperties", {}).get("private", {}).get(TAG),
        }

    def _body(self, fields, copy_of, src_label):
        when = (lambda d: {"date": d}) if fields["all_day"] else (lambda t: {"dateTime": t, "timeZone": "UTC"})
        return {
            "summary": fields["title"], "location": fields["location"],
            "description": f"Synced from {src_label} by calsync. Edit the original there; changes here are overwritten.",
            "start": when(fields["start"]), "end": when(fields["end"]),
            "transparency": "opaque" if fields["busy"] else "transparent",
            "reminders": {"useDefault": False, "overrides": []},
            "extendedProperties": {"private": {TAG: copy_of}},
        }

    def create(self, fields, copy_of, src_label):
        return http("POST", self._url(), self.token(), self._body(fields, copy_of, src_label))["id"]

    def update(self, uid, fields, copy_of, src_label):
        http("PUT", self._url("/" + urllib.parse.quote(uid, safe="")), self.token(), self._body(fields, copy_of, src_label))

    def create_native(self, ev):
        """A real event (from quick add), not a copy: untagged, so the next sync copies it everywhere."""
        when = (lambda d: {"date": d}) if ev["all_day"] else (lambda t: {"dateTime": t.isoformat(), "timeZone": tz_name()})
        return http("POST", self._url(), self.token(), {
            "summary": ev["title"], "start": when(ev["start"]), "end": when(ev["end"]),
            "description": "Added with calsync quick add.",
            "extendedProperties": {"private": {"calsync_quick": "1"}}})["id"]

    def delete(self, uid):
        try:
            http("DELETE", self._url("/" + urllib.parse.quote(uid, safe="")), self.token())
        except RuntimeError as e:
            if "-> 410" not in str(e) and "-> 404" not in str(e):
                raise


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
             "$expand": f"singleValueExtendedProperties($filter=id eq '{MS_PROP}')"}
        url = self._cal() + "/calendarView?" + urllib.parse.urlencode(q)
        while url:
            r = http("GET", url, self.token(), headers={"Prefer": 'outlook.timezone="UTC"'})
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
        tag = next((p["value"] for p in it.get("singleValueExtendedProperties", []) if p.get("id", "").lower() == MS_PROP.lower()), None)
        return {
            "uid": it["id"], "ical": it.get("iCalUId"), "title": it.get("subject", ""),
            "start": when(it["start"]), "end": when(it["end"]), "all_day": all_day,
            "busy": it.get("showAs") not in ("free", "workingElsewhere"),
            "location": (it.get("location") or {}).get("displayName", ""),
            "private": it.get("sensitivity") in ("private", "confidential"),
            "declined": (it.get("responseStatus") or {}).get("response") == "declined",
            "copy_of": tag,
        }

    def _body(self, fields, copy_of, src_label):
        t = (lambda d: {"dateTime": d + "T00:00:00", "timeZone": "UTC"}) if fields["all_day"] else (lambda z: {"dateTime": z[:-1], "timeZone": "UTC"})
        return {
            "subject": fields["title"], "location": {"displayName": fields["location"]},
            "body": {"contentType": "text", "content": f"Synced from {src_label} by calsync. Edit the original there; changes here are overwritten."},
            "start": t(fields["start"]), "end": t(fields["end"]), "isAllDay": fields["all_day"],
            "showAs": "busy" if fields["busy"] else "free", "isReminderOn": False,
            "singleValueExtendedProperties": [{"id": MS_PROP, "value": copy_of}],
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
        declined = any(p.get("PARTSTAT", "").upper() == "DECLINED" and v.lower().removeprefix("mailto:") in me for p, v in ev.get("ATTENDEE", []))
        return {"uid": f"{uid}|{orig}", "ical": uid, "title": _unescape(first(ev, "SUMMARY")[1]),
                "start": fs, "end": fe, "all_day": all_day, "busy": busy,
                "location": _unescape(first(ev, "LOCATION")[1]),
                "private": first(ev, "CLASS")[1].upper() in ("PRIVATE", "CONFIDENTIAL"),
                "declined": declined, "copy_of": tag}

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


def due_items(evs, tmin, tmax, only=None, free=False):
    """Shape a due-date feed, such as Canvas assignments. --only keeps items whose UID holds that text; --free stops
    them marking anyone busy, since a due date isn't a meeting. A due time with no length ends at the deadline, so it
    can be seen. All-day items are held to what Google will list back: it hides one once its day is over and doesn't
    reach one just past the window, and ics_events keeps both, which would make a new copy on every run."""
    lo = dt.datetime.fromisoformat(tmin.replace("Z", "+00:00"))
    last = dt.datetime.fromisoformat(tmax.replace("Z", "+00:00")).date() - dt.timedelta(days=2)
    out = []
    for ev in evs:
        if only and only not in (ev.get("ical") or ""):
            continue
        if ev["all_day"]:
            if dt.datetime.fromisoformat(ev["end"]).replace(tzinfo=local_tz()) <= lo or dt.date.fromisoformat(ev["start"]) >= last:
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

    def list(self, tmin, tmax):
        req = urllib.request.Request(self.url, headers={"User-Agent": "calsync"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:  # reading only part would look like the rest of the events were deleted
            raise RuntimeError("this calendar is over 50 MB, so calsync didn't read it")
        text = raw.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            raise RuntimeError("link did not return a calendar")
        evs = ics_events(text, tmin, tmax)
        return due_items(evs, tmin, tmax, self.only, self.free) if self.only or self.free else evs


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
                if ev["copy_of"]:
                    ev["uid"] = urllib.parse.urljoin(final, href)  # what update and delete need
                out.append(ev)
        return out

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
                 "DESCRIPTION:" + _ics_escape(f"Synced from {src_label} by calsync. Edit the original there; changes here are overwritten."),
                 f"{ICS_TAG}:{_ics_escape(copy_of)}"]
        if fields["location"]:
            lines.append("LOCATION:" + _ics_escape(fields["location"]))
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


def sync(cals, adapters, dry_run=False, force=False, log=print, now=None):
    """cals: list of calendar configs; adapters: name -> adapter. Returns a summary dict."""
    now = now or now_utc()
    tmin = (now - dt.timedelta(days=PAST_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmax = (now + dt.timedelta(days=FUTURE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listed, failed = {}, set()
    for c in cals:
        try:
            listed[c["name"]] = adapters[c["name"]].list(tmin, tmax)
        except Exception as e:  # one bad calendar must not stop the rest, nor delete its copies
            failed.add(c["name"])
            log(f"! {c['name']}: list failed: {e}")
    labels = {c["name"]: c.get("label") or c["name"] for c in cals}
    sources = {n: [e for e in evs if not e["copy_of"] and not e["declined"] and not _canceled(e)] for n, evs in listed.items()}
    summary = {"created": 0, "updated": 0, "deleted": 0, "skipped_deletes": 0, "failed": sorted(failed), "errors": 0}

    for dst in cals:
        name = dst["name"]
        if dst.get("read_only") or getattr(adapters[name], "read_only", False) or name in failed:
            continue
        ad = adapters[name]
        # A meeting you were invited to on several accounts already sits on dst natively: don't add a second copy.
        native = {e["ical"] for e in listed[name] if not e["copy_of"] and e.get("ical")}
        desired = {}
        for src, evs in sources.items():
            if src == name or src in dst.get("skip_sources", []):
                continue
            for ev in evs:
                if ev.get("ical") and ev["ical"] in native:
                    continue
                hide = dst.get("busy_only") or ev["private"]   # a Busy copy doesn't say which calendar it came from either
                desired[f"{src}|{ev['uid']}"] = (render(ev, src, dst), "another calendar" if hide else labels[src])
        existing = {}
        dupes = []
        for ev in listed[name]:
            if ev["copy_of"]:
                if ev["copy_of"] in existing:
                    dupes.append(ev)
                else:
                    existing[ev["copy_of"]] = ev
        to_delete = [ev for key, ev in existing.items()
                     if key not in desired and key.split("|", 1)[0] not in failed] + dupes
        total_copies = len(existing) + len(dupes)
        if to_delete and not force and len(to_delete) > max(10, total_copies // 2):
            log(f"! {name}: would delete {len(to_delete)} of {total_copies} copies; skipped (run with --force if intended)")
            summary["skipped_deletes"] += len(to_delete)
            to_delete = []

        for key, (fields, label) in desired.items():
            have = existing.get(key)
            try:
                if have is None:
                    log(f"+ {name}: {fields['title']} {fields['start']}")
                    if not dry_run:
                        ad.create(fields, key, label)
                    summary["created"] += 1
                elif comparable(have) != fields:
                    log(f"~ {name}: {fields['title']} {fields['start']}")
                    if not dry_run:
                        ad.update(have["uid"], fields, key, label)
                    summary["updated"] += 1
            except Exception as e:
                summary["errors"] += 1
                log(f"! {name}: write failed for {key}: {e}")
        for ev in to_delete:
            try:
                log(f"- {name}: {ev['title']} {ev['start']}")
                if not dry_run:
                    ad.delete(ev["uid"])
                summary["deleted"] += 1
            except Exception as e:
                summary["errors"] += 1
                log(f"! {name}: delete failed for {ev['uid']}: {e}")
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
    events = adapter.list(start.strftime("%Y-%m-%dT%H:%M:%SZ"),
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


def ics_file(name, label, events, stamp):
    """A calendar of expanded instances, each with its own UID. Same hiding as a copy: a private event says Busy.
    A location holding a link is left out, since join links can carry passcodes."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//calsync//export//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:" + _ics_escape(label)]
    for ev in events:
        f = render(ev, name, {})
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
        for ev in sorted(adapters[name].list(tmin, tmax), key=lambda e: (e["start"], e["uid"])):
            if ev["copy_of"] or ev["declined"] or _canceled(ev):  # the same rule sync uses for a source event
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

def run_once(dry_run=False, force=False, log=print):
    """One whole pass. Returns the summary, or None if a run is already going. Problems land in summary["problems"]."""
    with file_lock("run.lock", blocking=False) as free:  # one run at a time: the timer, `calsync new` and the web page can overlap
        if not free:
            return None
        cfg = load_json(CONFIG, {"calendars": []})
        cals = cfg["calendars"]
        started = now_utc()
        problems = []

        def note(line):
            if line.startswith("!"):
                problems.append(line)
            log(line)

        if len(cals) < 2:
            s = {"created": 0, "updated": 0, "deleted": 0, "skipped_deletes": 0, "failed": [], "errors": 0}
            note("Fewer than two calendars configured; nothing to sync.")
        else:
            adapters = {c["name"]: make_adapter(c) for c in cals}
            added, qa_errors, qa = 0, 0, cfg.get("quick_add")
            if qa and not dry_run:  # before the sync, so new events are copied everywhere in this same run
                try:
                    added = quick_add_note(qa["note"], adapters[qa["calendar"]], log=note)
                except Exception as e:
                    qa_errors = 1
                    note(f"! quick-add: {e}")
            s = sync(cals, adapters, dry_run=dry_run, force=force, log=note)
            s["errors"] += qa_errors
            s["quick_added"] = added
            ag = cfg.get("agenda")
            if ag and not dry_run:  # after the sync, so it shows the copies this run just made
                try:
                    s["agenda_files"] = write_agenda(ag, adapters[ag["calendar"]])
                except Exception as e:
                    s["errors"] += 1
                    note(f"! agenda: {e}")
            ex = cfg.get("export")
            if ex and not dry_run:
                try:
                    s["exported"] = export_calendars(ex, cals, adapters)
                except Exception as e:
                    s["errors"] += 1
                    note(f"! export: {e}")
        s.update(at=started.strftime("%Y-%m-%dT%H:%M:%SZ"), dry_run=dry_run, seconds=round((now_utc() - started).total_seconds(), 1), problems=problems[:20])
        if not dry_run:
            save_json(LASTRUN, s)
        return s


def cmd_run(dry_run, force):
    s = run_once(dry_run, force)
    if s is None:
        print("Another run is going; try again in a moment.")
        return 0
    print(json.dumps(s))
    return 1 if s["errors"] or s["failed"] else 0


def main_calendar(cfg):
    """Where quick add puts events: config quick_add.calendar, else the first calendar calsync can write to."""
    cals, want = cfg["calendars"], cfg.get("quick_add", {}).get("calendar")
    accounts = _accounts()
    for c in cals:
        writable = not c.get("read_only") and accounts.get(c.get("account"), {}).get("provider") != "ics"
        if c["name"] == want or (not want and writable):
            return c
    raise SystemExit("No calendar to add to yet: add one with `calsync add`.")


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
