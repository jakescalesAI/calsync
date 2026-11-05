"""CalDAV tests against a small real HTTP server: python3 -m unittest test_caldav"""
import base64, contextlib, datetime as dt, hashlib, http.server, io, json, os, re, tempfile, threading, unittest, urllib.parse
from unittest import mock

os.environ.setdefault("CALSYNC_HOME", tempfile.mkdtemp())    # never your real one
os.environ["CALSYNC_TZ"] = "America/New_York"
import calsync
from test_calsync import FakeCal

NOW = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
TMIN, TMAX = "2030-01-01T00:00:00Z", "2030-05-01T00:00:00Z"
USER, PASSWORD = "me@example.com", "app-pass-1234"
MAIN = "/dav/cals/me/main/"


def multistatus(*responses):
    return ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            + "".join(f'<d:response><d:href>{h}</d:href><d:propstat><d:prop>{p}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>' for h, p in responses)
            + "</d:multistatus>")


def collection(name, comp, rights):
    return ("<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            f'<d:displayname>{name}</d:displayname><c:supported-calendar-component-set><c:comp name="{comp}"/></c:supported-calendar-component-set>'
            f"<d:current-user-privilege-set><d:privilege><d:{rights}/></d:privilege></d:current-user-privilege-set>")


class Dav(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body="", headers=None):
        raw = body.encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        auth = self.headers.get("Authorization", "")
        s = self.server
        s.seen.append(auth)
        if s.digest:
            f = dict(re.findall(r'(\w+)="?([^",]+)"?', auth))
            md5 = lambda x: hashlib.md5(x.encode()).hexdigest()
            good = auth.startswith("Digest ") and f.get("response") == md5(
                f"{md5(f'{USER}:calsync-test:{PASSWORD}')}:n0nce:{f.get('nc')}:{f.get('cnonce')}:auth:{md5(self.command + ':' + self.path)}")
            if not good:
                self.reply(401, headers={"WWW-Authenticate": 'Digest realm="calsync-test", nonce="n0nce", qop="auth"'})
            return good
        good = auth == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if not good:
            self.reply(401, headers={"WWW-Authenticate": 'Basic realm="calsync-test"'})
        return good

    def do_PROPFIND(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        s = self.server
        if self.path == "/" and s.root_missing:
            return self.reply(404)
        if self.path == "/.well-known/caldav":
            return self.reply(301, headers={"Location": s.elsewhere or "/dav/"})
        if not self.allowed():
            return
        if self.path in ("/", "/dav/"):
            return self.reply(207, multistatus((self.path, "<d:current-user-principal><d:href>/dav/principals/me/</d:href></d:current-user-principal>")))
        if self.path == "/dav/principals/me/":
            return self.reply(207, multistatus((self.path, "<c:calendar-home-set><d:href>/dav/cals/me/</d:href></c:calendar-home-set>"
                                                f"<c:calendar-user-address-set><d:href>mailto:{USER}</d:href><d:href>mailto:Other@Example.com</d:href></c:calendar-user-address-set>")))
        if self.path == "/dav/cals/me/":
            return self.reply(207, multistatus(("/dav/cals/me/", "<d:resourcetype><d:collection/></d:resourcetype>"),
                                               (MAIN, collection("Home &amp; family", "VEVENT", "write")),
                                               ("/dav/cals/me/shared/", collection("Shared", "VEVENT", "read")),
                                               ("/dav/cals/me/tasks/", collection("Reminders", "VTODO", "write"))))
        self.reply(404)

    def do_REPORT(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.allowed():
            esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;")
            self.reply(207, multistatus(*[(h, f'<d:getetag>"1"</d:getetag><c:calendar-data>{esc(t)}</c:calendar-data>')
                                          for h, t in self.server.items.items() if h.startswith(self.path)]))

    def do_PUT(self):
        text = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.allowed():
            if self.server.drops_tag:
                text = re.sub(r"X-CALSYNC:.*\r\n( .*\r\n)*", "", text)
            created = self.path not in self.server.items
            self.server.items[self.path] = text
            self.reply(201 if created else 204)

    def do_GET(self):
        if self.allowed():
            self.reply(200, self.server.items[self.path]) if self.path in self.server.items else self.reply(404)

    def do_DELETE(self):
        if self.allowed():
            self.reply(204 if self.server.items.pop(self.path, None) else 404)


def event(uid, start="20300110T150000Z", end="20300110T160000Z", extra=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20300101T000000Z\r\nDTSTART:{start}\r\nDTEND:{end}\r\nSUMMARY:{uid}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


class Base(unittest.TestCase):
    host = "127.0.0.1"

    def serve(self, host=None):
        s = http.server.ThreadingHTTPServer((host or self.host, 0), Dav)
        s.items, s.seen = {}, []
        s.digest = s.drops_tag = s.root_missing = False
        s.elsewhere = None
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(s.server_close)
        self.addCleanup(s.shutdown)
        return s

    def setUp(self):
        self.dav = self.serve()
        self.url = f"http://{self.host}:{self.dav.server_address[1]}/"
        self.acct = {"server": self.url, "username": USER, "password": PASSWORD, "addresses": [USER, "other@example.com"]}
        self.cal = calsync.CalDavCalendar(self.acct, self.url + MAIN.lstrip("/"))
        p = os.path.join(calsync.SECRETS, "accounts.json")
        if os.path.exists(p):
            os.remove(p)

    def sync(self, cals, cfg=None, **kw):
        cfgs = [dict({"name": n}, **(cfg or {}).get(n, {})) for n in cals]
        return calsync.sync(cfgs, cals, now=NOW, log=lambda *_: None, **kw)

    def copies(self):
        return [e for e in self.cal.list(TMIN, TMAX) if e["copy_of"]]


class ConnectTests(Base):
    def test_finds_calendars_through_well_known(self):
        self.dav.root_missing = True
        found = calsync.caldav_connect("home", self.url, USER, PASSWORD, "Fastmail")
        self.assertEqual([(c["name"], c["writable"]) for c in found], [("Home & family", True), ("Shared", False)])
        self.assertEqual(found[0]["id"], self.url + MAIN.lstrip("/"))
        acct = calsync._accounts()["home"]
        self.assertEqual((acct["provider"], acct["service"], acct["addresses"]), ("caldav", "Fastmail", [USER, "other@example.com"]))
        self.assertEqual(calsync.account_calendars("home"), found)

    def test_digest_servers_work(self):
        self.dav.digest = True
        self.assertEqual(len(calsync.caldav_connect("home", self.url, USER, PASSWORD)), 2)
        self.assertEqual(len(self.cal.list(TMIN, TMAX)), 0)

    def test_wrong_password_saves_nothing(self):
        with self.assertRaisesRegex(RuntimeError, "username or password"):
            calsync.caldav_connect("home", self.url, USER, "wrong")
        self.assertNotIn("home", calsync._accounts())

    def test_a_redirect_to_another_site_never_gets_the_password(self):
        other = self.serve("localhost")
        self.dav.root_missing = True
        self.dav.elsewhere = f"http://localhost:{other.server_address[1]}/dav/"
        with self.assertRaisesRegex(RuntimeError, "another site"):
            calsync.caldav_connect("home", self.url, USER, PASSWORD)
        self.assertEqual(other.seen, [])

    def test_a_password_never_goes_over_plain_http_to_the_internet(self):
        with self.assertRaisesRegex(RuntimeError, "https"):
            calsync.DavClient("http://caldav.example.com/", USER, PASSWORD)
        for ok in ("http://192.168.1.5:5232/", "http://radicale:5232/", "http://nas.local/", "cloud.example.com"):
            calsync.DavClient(ok, USER, PASSWORD)

    def test_https_cannot_be_redirected_down_to_http(self):
        client = calsync.DavClient("https://caldav.example.com/", USER, PASSWORD)
        self.assertTrue(client._trusted("https://caldav.example.com:443/1/calendars/"))
        self.assertFalse(client._trusted("http://caldav.example.com/"))
        self.assertFalse(client._trusted("https://caldav.example.org/"))

    def test_a_password_follows_only_its_own_host_or_a_built_in_provider(self):
        for typed, there in (("https://caldav.icloud.com/", "https://p12-caldav.icloud.com:443/1/"),
                             ("https://caldav.fastmail.com/", "https://d9.caldav.fastmail.com/"),
                             ("https://example.com/", "https://dav.example.com/"),
                             ("http://192.168.1.5:5232/", "http://192.168.1.5:5232/cal/")):
            self.assertTrue(calsync.DavClient(typed, USER, PASSWORD)._trusted(there), there)
        for typed, there in (("https://cal.example.co.uk/", "https://evil.co.uk/"),
                             ("https://me.duckdns.org/", "https://evil.duckdns.org/"),
                             ("https://cloud.example.com/", "https://other.example.com/"),
                             ("https://caldav.icloud.com/", "https://icloud.com.example.org/"),
                             ("https://caldav.icloud.com/", "https://notricloud.com/"),
                             ("http://192.168.1.5/", "http://10.0.1.5/")):
            self.assertFalse(calsync.DavClient(typed, USER, PASSWORD)._trusted(there), there)

    def test_errors_never_hold_the_password(self):
        self.dav.items[MAIN + "a.ics"] = "not a calendar"
        client = calsync.DavClient(self.url, USER, PASSWORD)
        status, raw, _ = client.request("GET", self.url + "nope")
        self.assertEqual(status, 404)
        self.assertNotIn(PASSWORD.encode(), raw)

    def test_every_preset_has_an_https_server_or_asks_for_one(self):
        for name, server, note in calsync.CALDAV_SERVERS.values():
            self.assertTrue(server is None or server.startswith("https://"), name)
            self.assertTrue(note)


class SyncTests(Base):
    def test_copies_land_and_a_second_run_changes_nothing(self):
        g = FakeCal()
        g.add('Lunch, with "Sam"; maybe', "2030-01-10T15:00:00Z", "2030-01-10T16:00:00Z", location="Cafe, Main St")
        g.add("Trip", "2030-01-12", "2030-01-14")
        g.add("Hold", "2030-01-15T15:00:00Z", "2030-01-15T16:00:00Z", busy=False)
        g.add("x" * 200 + " é", "2030-01-16T15:00:00Z", "2030-01-16T16:00:00Z")
        self.assertEqual(self.sync({"g": g, "dav": self.cal})["created"], 4)
        by_title = {e["title"]: e for e in self.copies()}
        self.assertEqual(by_title['Lunch, with "Sam"; maybe']["location"], "Cafe, Main St")
        self.assertEqual((by_title["Trip"]["start"], by_title["Trip"]["end"], by_title["Trip"]["all_day"]), ("2030-01-12", "2030-01-14", True))
        self.assertFalse(by_title["Hold"]["busy"])
        s = self.sync({"g": g, "dav": self.cal})
        self.assertEqual((s["created"], s["updated"], s["deleted"], s["errors"]), (0, 0, 0, 0))
        self.assertFalse(g.copies())

    def test_a_moved_original_moves_the_copy_and_a_deleted_one_removes_it(self):
        g = FakeCal()
        uid = g.add("Lunch", "2030-01-10T15:00:00Z", "2030-01-10T16:00:00Z")
        self.sync({"g": g, "dav": self.cal})
        g.events[uid]["start"], g.events[uid]["end"] = "2030-01-11T15:00:00Z", "2030-01-11T16:00:00Z"
        self.assertEqual(self.sync({"g": g, "dav": self.cal})["updated"], 1)
        self.assertEqual(self.copies()[0]["start"], "2030-01-11T15:00:00Z")
        self.assertEqual(len(self.dav.items), 1)
        del g.events[uid]
        self.assertEqual(self.sync({"g": g, "dav": self.cal})["deleted"], 1)
        self.assertEqual(self.dav.items, {})

    def test_events_on_the_calendar_are_copied_out_and_left_alone(self):
        self.dav.items[MAIN + "mine.ics"] = before = event("mine")
        g = FakeCal()
        g.add("Lunch", "2030-01-20T15:00:00Z", "2030-01-20T16:00:00Z")
        self.sync({"g": g, "dav": self.cal})
        self.assertEqual([e["title"] for e in g.copies()], ["mine"])
        self.assertEqual(self.dav.items[MAIN + "mine.ics"], before)
        with mock.patch("calsync.now_utc", return_value=NOW):
            self.assertEqual(calsync.purge([{"name": "dav"}], {"dav": self.cal}, log=lambda *_: None)["deleted"], 1)
        self.assertEqual(list(self.dav.items), [MAIN + "mine.ics"])

    def test_repeating_events_become_occurrences(self):
        self.dav.items[MAIN + "r.ics"] = event("standup", extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n")
        self.dav.items[MAIN + "d.ics"] = event("dates", extra="RDATE:20300112T150000Z,20300114T150000Z\r\n")
        g = FakeCal()
        self.sync({"g": g, "dav": self.cal})
        self.assertEqual(sorted(e["title"] for e in g.copies()), ["dates"] * 3 + ["standup"] * 3)

    def test_a_declined_meeting_stays_out(self):
        self.dav.items[MAIN + "a.ics"] = event("nope", extra="ATTENDEE;PARTSTAT=DECLINED:mailto:other@example.com\r\n")
        self.dav.items[MAIN + "b.ics"] = event("yes", extra="ATTENDEE;PARTSTAT=DECLINED:mailto:someone@else.com\r\n")
        g = FakeCal()
        self.sync({"g": g, "dav": self.cal})
        self.assertEqual([e["title"] for e in g.copies()], ["yes"])

    def test_private_and_busy_only_copies_say_busy(self):
        g = FakeCal()
        g.add("Therapy", "2030-01-10T15:00:00Z", "2030-01-10T16:00:00Z", location="Office")
        self.sync({"g": g, "dav": self.cal}, {"dav": {"busy_only": True}})
        text = "".join(self.dav.items.values())
        self.assertIn("SUMMARY:Busy", text)
        self.assertNotIn("Therapy", text)
        self.assertNotIn("Office", text)

    def test_a_copy_has_no_reminder(self):
        g = FakeCal()
        g.add("Lunch", "2030-01-10T15:00:00Z", "2030-01-10T16:00:00Z")
        self.sync({"g": g, "dav": self.cal})
        self.assertNotIn("VALARM", "".join(self.dav.items.values()))

    def test_a_server_that_drops_the_tag_gets_no_copies(self):
        self.dav.drops_tag = True
        g = FakeCal()
        g.add("Lunch", "2030-01-10T15:00:00Z", "2030-01-10T16:00:00Z")
        g.add("Dinner", "2030-01-11T15:00:00Z", "2030-01-11T16:00:00Z")
        lines = []
        s = calsync.sync([{"name": "g"}, {"name": "dav"}], {"g": g, "dav": self.cal}, now=NOW, log=lines.append)
        self.assertGreaterEqual(s["errors"], 1)
        self.assertTrue(any("drops calsync's tag" in x for x in lines))
        self.assertEqual(self.dav.items, {})

    def test_a_copy_that_lost_its_tag_is_still_never_copied_onward(self):
        self.dav.items[MAIN + "c.ics"] = event(calsync.COPY_UID + "abc")
        g = FakeCal()
        self.sync({"g": g, "dav": self.cal})
        self.assertEqual(g.copies(), [])

    def test_quick_add_is_a_real_event(self):
        start = dt.datetime(2030, 1, 10, 15, tzinfo=dt.timezone.utc)
        self.cal.create_native({"title": "Dentist", "all_day": False, "start": start, "end": start + dt.timedelta(hours=1)})
        self.cal.create_native({"title": "Holiday", "all_day": True, "start": "2030-01-12", "end": "2030-01-13"})
        evs = {e["title"]: e for e in self.cal.list(TMIN, TMAX)}
        self.assertIsNone(evs["Dentist"]["copy_of"])
        self.assertEqual(evs["Dentist"]["start"], "2030-01-10T15:00:00Z")
        self.assertEqual(evs["Holiday"]["start"], "2030-01-12")

    def test_a_calendar_that_cannot_load_keeps_its_copies_elsewhere(self):
        g = FakeCal()
        self.dav.items[MAIN + "mine.ics"] = event("mine")
        self.sync({"g": g, "dav": self.cal})
        self.assertEqual(len(g.copies()), 1)
        broken = calsync.CalDavCalendar(dict(self.acct, password="wrong"), self.cal.url)
        s = self.sync({"g": g, "dav": broken})
        self.assertEqual((s["failed"], s["deleted"]), (["dav"], 0))
        self.assertEqual(len(g.copies()), 1)


class MakeAdapterTests(Base):
    def test_caldav_account_makes_an_adapter(self):
        calsync.caldav_connect("home", self.url, USER, PASSWORD)
        ad = calsync.make_adapter({"account": "home", "calendar_id": self.url + MAIN.lstrip("/")})
        self.assertIsInstance(ad, calsync.CalDavCalendar)
        self.assertEqual(ad.list(TMIN, TMAX), [])

    def test_cli_adds_an_account_without_echoing_the_password(self):
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=[self.url, USER]), mock.patch("getpass.getpass", return_value=PASSWORD), contextlib.redirect_stdout(out):
            self.assertEqual(calsync.main(["auth", "caldav", "home"]), 0)
        self.assertIn("Home & family", out.getvalue())
        self.assertNotIn(PASSWORD, out.getvalue())
        self.assertEqual(oct(os.stat(os.path.join(calsync.SECRETS, "accounts.json")).st_mode & 0o777), "0o600")

    def test_cli_asks_for_the_address_of_a_service_with_no_fixed_server(self):
        with mock.patch("builtins.input", side_effect=[self.url, USER]) as asked, mock.patch("getpass.getpass", return_value=PASSWORD), contextlib.redirect_stdout(io.StringIO()):
            calsync.main(["auth", "caldav", "home", "nextcloud"])
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(calsync._accounts()["home"]["service"], "Nextcloud")


if __name__ == "__main__":
    unittest.main()
