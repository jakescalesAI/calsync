"""Web page tests against a real local server and in-memory calendars: python3 -m unittest test_web"""
import http.client, json, os, re, socket, stat, tempfile, threading, time, unittest, urllib.parse
from unittest import mock

home = os.environ.setdefault("CALSYNC_HOME", tempfile.mkdtemp())    # never your real one
os.environ["CALSYNC_TZ"] = "America/New_York"
import calsync, calsync_web


class Fake:
    def __init__(self):
        self.events = {}

    def list(self, tmin, tmax):
        return [dict(e) for e in self.events.values()]

    def create(self, fields, copy_of, label):
        uid = f"c{len(self.events)}"
        self.events[uid] = dict(fields, uid=uid, ical=uid, private=False, declined=False, copy_of=copy_of)
        return uid

    def update(self, uid, fields, copy_of, label):
        self.events[uid].update(fields)

    def delete(self, uid):
        del self.events[uid]


def native(title):
    return {"uid": title, "ical": title, "title": title, "start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z",
            "all_day": False, "busy": True, "location": "", "private": False, "declined": False, "copy_of": None}


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = calsync_web.Server(("127.0.0.1", 0), calsync_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        for root, dirs, files in os.walk(home):
            for f in files:
                os.remove(os.path.join(root, f))
        calsync_web.pending_google.clear()
        calsync_web.pending_ops.clear()      # an edit queued by one test must never land in the next
        self.cookie = None

    def req(self, method, path, form=None, host=None, origin=None, cookie=True, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"localhost:{self.port}", **(headers or {})}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if origin:
            h["Origin"] = origin
        elif method == "POST" and origin is None:      # what a browser sends with this page's own forms
            h["Origin"] = "http://" + h["Host"]
        if cookie and self.cookie:
            h["Cookie"] = "calsync_browser=" + self.cookie
        c.request(method, path, body, h)
        r = c.getresponse()
        text = r.read().decode()
        got = r.getheader("Set-Cookie")
        if got and cookie:
            self.cookie = got.split(";")[0].split("=", 1)[1] or None
        c.close()
        return r, text

    def start(self):
        """The first visit: no password, just a browser ID for the form tokens."""
        r, _ = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIsNotNone(self.cookie)

    def csrf(self, path="/settings"):
        return re.search(r'name="csrf" value="(\w+)"', self.req("GET", path)[1]).group(1)

    def post(self, path, **form):
        return self.req("POST", path, dict(form, csrf=self.csrf()))

    # --- forgotten password, the merged sync page and the Calendar tab

    def test_adding_a_calendar_is_on_the_sync_page(self):
        self.start()
        text = self.req("GET", "/")[1]
        self.assertIn('id="add"', text)
        self.assertIn('href="/calendar"', text)
        r, _ = self.req("GET", "/connect")
        self.assertEqual((r.status, r.getheader("Location")), (303, "/#add"))

    def test_quick_add_previews_warns_then_adds(self):
        self.start()
        calls = []
        plan_ev = {"title": "Breakfast", "when": "Sat Nov 28, 9:00-11:00 AM", "all_day": False, "guests": ["sam@x.com"], "remind_minutes": 60}
        clash = [{"title": "CSCI-C 455", "when": "9:30-10:45 AM", "calendar": "school"}]
        with mock.patch.object(calsync, "writable_calendars", return_value=[{"name": "work", "label": "work"}]), \
             mock.patch.object(calsync, "make_adapter", return_value=calsync.GoogleCalendar("a", "b")), \
             mock.patch.object(calsync, "busy_overlaps", return_value=(clash, [])), \
             mock.patch.object(calsync, "quick_add", side_effect=lambda *a, **k: calls.append((a, k)) or {"status": "added", "synced": True}), \
             mock.patch.object(calsync_web, "calendar_page", side_effect=lambda csrf, q, flash=None, quick=None: calsync_web.page("C", (flash or "") + (quick or ""), True)):
            _, text = self.post("/quick", text="Breakfast with sam@x.com sat 9am 2h remind me 1h before", calendar="work", back="/calendar?view=week")
            self.assertIn("This overlaps CSCI-C 455", text)
            self.assertIn("Book anyway", text)
            self.assertIn("sam@x.com", text)
            self.assertIn('name="send_invites" value="1" required', text)
            self.assertEqual(calls, [])                                     # a preview writes nothing
            r, _ = self.post("/quick/add", text="Breakfast with sam@x.com sat 9am 2h", calendar="work", book_anyway="1", send_invites="1", back="https://evil.example/")
            self.assertEqual((r.status, r.getheader("Location")), (303, "/calendar?m=quick"))
            self.assertEqual(calls[0][1], {"confirm_overlap": True, "confirm_guests": True})

    def test_skip_and_undo_go_through_the_engine(self):
        self.start()
        with mock.patch.object(calsync, "skip_occurrence") as skip, mock.patch.object(calsync, "unskip_occurrence") as unskip:
            r, _ = self.post("/skip", key="school|abc|1", title="Class", start="2026-11-30T17:00:00Z", back="/calendar?view=week&d=2026-11-29")
            self.assertTrue(r.getheader("Location").startswith("/calendar?view=week&d=2026-11-29&m=skipped"))
            skip.assert_called_once_with("school|abc|1", "Class", "2026-11-30T17:00:00Z")
            self.post("/unskip", key="school|abc|1", back="/calendar")
            unskip.assert_called_once_with("school|abc|1")

    def test_the_calendar_tab_shows_events_in_each_calendars_colour(self):
        self.start()
        calsync.update_config(lambda c: c["calendars"].extend([
            {"name": "work", "label": "work", "account": "a", "calendar_id": "x"}, {"name": "home", "label": "home", "account": "b", "calendar_id": "y"}]))
        today = calsync.dt.datetime.now(calsync.local_tz()).date()
        at = calsync.dt.datetime.combine(today, calsync.dt.time(12), calsync.local_tz()).astimezone(calsync.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        own = {"uid": "1", "ical": "1", "title": "Dentist <b>", "start": at, "end": at, "all_day": False, "busy": True, "location": "", "private": False, "declined": False, "copy_of": None}
        copy = dict(own, uid="2", title="Dentist <b>", copy_of="work|1")

        class Fake:
            def __init__(self, evs):
                self.evs = evs

            def list(self, tmin, tmax):
                return self.evs
        calsync_web.CAL_CACHE.clear()
        with mock.patch.object(calsync, "make_adapter", side_effect=lambda c: Fake([own] if c["name"] == "work" else [copy])):
            text = self.req("GET", "/calendar")[1]
            self.assertEqual(text.count("Dentist &lt;b&gt;</a>"), 1)      # Everything: the copy on home isn't shown twice
            self.assertIn('class="ev k0 s', text)
            calsync_web.CAL_CACHE.clear()
            text = self.req("GET", "/calendar?view=month&show=home")[1]
            self.assertIn('class="ev k0 copy"', text)                  # a copy, in the colour of where it came from
            self.assertIn("/event?cal=work&amp;uid=1", text)             # opening the copy opens its original
        with mock.patch.object(calsync, "make_adapter", side_effect=RuntimeError("no way in")):
            calsync_web.CAL_CACHE.clear()
            self.assertIn("no way in", self.req("GET", "/calendar")[1])

    # --- CalDAV

    def test_connect_lists_every_calendar_service(self):
        self.start()
        text = self.req("GET", "/")[1]
        self.assertIn("Calendly, Cal.com or another booking app", text)
        for name in ("Apple iCloud", "Fastmail", "Yahoo Calendar", "AOL Calendar", "Zoho Calendar", "Nextcloud", "Another CalDAV server", "Proton Calendar"):
            self.assertIn(name, text)

    def test_caldav_sign_in_is_saved_and_the_password_never_comes_back(self):
        self.start()
        found = [{"id": "https://dav.example.com/cals/home/", "name": "Home", "writable": True}]

        def connect(label, server, username, password, service=""):
            self.assertEqual((server, service), ("https://caldav.icloud.com/", "Apple iCloud"))
            calsync._save_account(label, {"provider": "caldav", "service": service, "server": server, "username": username, "password": password, "homes": []})
            return found
        with mock.patch.object(calsync, "caldav_connect", connect):
            self.post("/connect/caldav/save", kind="icloud", label="apple", username="me@example.com", password="SECRET-APP-PASS")
        with mock.patch.object(calsync, "account_calendars", return_value=found):
            for path in ("/", "/connect", "/connect/caldav?p=icloud", "/account?label=apple"):
                self.assertNotIn("SECRET-APP-PASS", self.req("GET", path)[1])
            self.assertIn("Apple iCloud", self.req("GET", "/")[1])
            self.assertIn("Home", self.req("GET", "/account?label=apple")[1])

    def test_a_bad_caldav_sign_in_shows_why_and_saves_nothing(self):
        self.start()
        with mock.patch.object(calsync, "caldav_connect", side_effect=RuntimeError("the server turned down that username or password")):
            _, text = self.post("/connect/caldav/save", kind="fastmail", label="mail", username="me", password="x")
        self.assertIn("turned down", text)
        self.assertNotIn("mail", calsync._accounts())

    def test_own_caldav_server_asks_for_an_address(self):
        self.start()
        self.assertIn('name="server"', self.req("GET", "/connect/caldav?p=other")[1])
        self.assertNotIn('name="server"', self.req("GET", "/connect/caldav?p=icloud")[1])

    # --- who gets in

    def test_it_opens_straight_in_and_files_are_private(self):
        r, text = self.req("GET", "/")
        self.assertIn("Nothing is connected", text)
        for gone in ("Choose a password", "Sign in</h1>", "Forgot password", "recovery code", "Setup code"):
            self.assertNotIn(gone, text)
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(calsync.SECRETS, "page-key.json")).st_mode), 0o600)
        for path in ("/first-run", "/login", "/forgot", "/settings/password", "/settings/recovery", "/logout"):
            self.assertEqual(self.req("GET", path)[0].status, 404, path)

    def test_an_old_page_password_is_left_alone_and_never_asked_for(self):
        old = {"salt": "00" * 16, "hash": "ab" * 64, "key": "cd" * 32, "recovery": {"salt": "00" * 16, "hash": "ef" * 64, "made": "2026-11-01"}}
        calsync.ensure_home()
        calsync.save_json(os.path.join(calsync.SECRETS, "web.json"), old)
        self.start()
        text = self.req("GET", "/settings")[1]
        self.assertNotIn("password", text.lower().replace("app password", ""))
        self.assertEqual(self.post("/start")[0].status, 303)
        self.assertEqual(calsync.load_json(os.path.join(calsync.SECRETS, "web.json"), None), old)

    def test_every_change_needs_this_browsers_form_token(self):
        self.start()
        token = self.csrf()
        for path in ("/run", "/start", "/preview", "/purge", "/connect/google/client", "/calendar/prefs"):
            self.assertEqual(self.req("POST", path, {"csrf": "x"})[0].status, 400, path)
            self.assertEqual(self.req("POST", path, {"csrf": token}, cookie=False)[0].status, 400, path)   # another browser can't borrow it
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))

    def test_csrf_origin_and_host_are_checked(self):
        self.start()
        self.assertEqual(self.req("POST", "/start", {"csrf": "wrong"})[0].status, 400)
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))
        token = self.csrf()
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="https://evil.example")[0].status, 400)
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="null")[0].status, 400)  # a sandboxed or referrer-less page
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="")[0].status, 400)      # no Origin and no Referer
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="", headers={"Referer": "https://evil.example/x"})[0].status, 400)
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))
        # DNS rebinding: someone's name pointed at 127.0.0.1, another port, or this machine's address on the network
        for host in ("evil.example", f"evil.example:{self.port}", "localhost:1", f"localhost.evil.example:{self.port}",
                     f"192.168.1.20:{self.port}", f"app.localhost:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.req("GET", "/", host=host)[0].status, 400, host)
            self.assertEqual(self.req("POST", "/start", {"csrf": token}, host=host, origin=f"http://{host}")[0].status, 400, host)
        self.assertEqual(self.req("GET", "/", host=f"127.0.0.1:{self.port}")[0].status, 200)
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="", headers={"Referer": f"http://localhost:{self.port}/"})[0].status, 303)
        self.assertTrue(calsync.load_json(calsync.CONFIG, {}).get("enabled"))

    def test_no_cors_headers(self):
        r, _ = self.req("GET", "/", headers={"Origin": "https://evil.example"})
        self.assertFalse([h for h in r.getheaders() if h[0].lower().startswith("access-control-")])
        r, _ = self.req("OPTIONS", "/", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertNotEqual(r.status, 200)
        self.assertFalse([h for h in r.getheaders() if h[0].lower().startswith("access-control-")])

    def test_it_only_listens_beyond_this_computer_when_told_to(self):
        for bind in ("127.0.0.1", "localhost", "::1"):
            calsync_web.local_only(bind)
        with mock.patch.object(calsync_web, "ALLOW_REMOTE", False):
            with self.assertRaises(SystemExit) as no:
                calsync_web.local_only("0.0.0.0")
            self.assertIn("CALSYNC_ALLOW_REMOTE=1", str(no.exception))
        with mock.patch.object(calsync_web, "ALLOW_REMOTE", True), mock.patch("builtins.print") as said:
            calsync_web.local_only("0.0.0.0")
        self.assertIn("WARNING", said.call_args[0][0])
        # allowed remote, it answers to the names you list and to a published port
        with mock.patch.object(calsync_web, "ALLOW_REMOTE", True), mock.patch.object(calsync_web, "EXTRA_HOSTS", {"cal.home.arpa"}):
            self.assertTrue(calsync_web.host_allowed("cal.home.arpa:9000", 8484))
            self.assertTrue(calsync_web.host_allowed("localhost:9000", 8484))
            self.assertFalse(calsync_web.host_allowed("evil.example:8484", 8484))
        self.assertFalse(calsync_web.host_allowed("cal.home.arpa:8484", 8484))

    def test_security_headers_and_no_scripts(self):
        r, text = self.req("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe", csp)
        # not no-referrer: that makes browsers send "Origin: null" on the page's own forms (see below)
        self.assertEqual(r.getheader("Referrer-Policy"), "same-origin")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(re.findall(r"<script[^>]*>", text), ['<script src="/app.js" defer>'])    # the one same-origin script, for the password eye
        js = self.req("GET", "/app.js")[1]
        for bit in ("input[type=password][data-reveal]", "'Show '", "'Hide '", "aria-pressed", "b.type='button'"):
            self.assertIn(bit, js)
        icloud = self.req("GET", "/connect/caldav?p=icloud")[1]       # the eye is on the app-password field only
        self.assertIn('type="password" id="password" name="password" required autocomplete="new-password" data-reveal', icloud)
        self.assertEqual(self.req("GET", "/connect/ics")[1].count("data-reveal"), 0)
        self.assertNotIn("<style", text)

    def test_cookie_flags(self):
        r, _ = self.req("GET", "/")
        flags = r.getheader("Set-Cookie")
        self.assertTrue(flags.startswith("calsync_browser="))
        self.assertIn("HttpOnly", flags)
        self.assertIn("SameSite=Lax", flags)
        self.assertIsNone(self.req("GET", "/")[0].getheader("Set-Cookie"))     # once a browser has one, it keeps it

    def test_client_secret_and_ics_link_never_come_back(self):
        self.start()
        self.post("/connect/google/client", client_id="my-client-id", client_secret="SECRET-VALUE-123")
        with mock.patch.object(calsync.IcsCalendar, "list", return_value=[]):
            self.post("/connect/ics/save", label="classes", url="https://example.com/private/TOKEN-456/cal.ics")
        for path in ("/", "/connect", "/connect/google", "/connect/ics", "/settings", "/account?label=classes"):
            text = self.req("GET", path)[1]
            self.assertNotIn("SECRET-VALUE-123", text, path)
            self.assertNotIn("TOKEN-456", text, path)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(calsync.SECRETS, "accounts.json")).st_mode), 0o600)

    def test_ics_rejects_non_web_links(self):
        self.start()
        _, text = self.post("/connect/ics/save", label="bad", url="file:///etc/passwd")
        self.assertIn("https://", text)
        self.assertNotIn("bad", calsync._accounts())
        self.assertEqual(calsync.clean_ics_url("webcal://example.com/a.ics"), "https://example.com/a.ics")

    def test_text_is_escaped(self):
        self.start()
        calsync._save_account("acct", {"provider": "ics", "url": "https://example.com/x.ics"})
        evil = "<script>alert(1)</script>"
        with mock.patch.object(calsync, "account_calendars", return_value=[{"id": 'x"><b>', "name": evil, "writable": True}]):
            text = self.req("GET", "/account?label=acct")[1]
        self.assertNotIn(evil, text)
        self.assertNotIn('"><b>', text)

    def test_preview_escapes_event_titles(self):
        self.start()
        self.two_calendars()
        self.cals["a"].events["e"] = native("<img src=x onerror=alert(1)>")
        with mock.patch.object(calsync, "make_adapter", self.adapter):
            self.post("/preview")
            text = self.req("GET", "/")[1]
        self.assertIn("1 to add", text)
        self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;img src=x", text)

    # --- Google sign-in

    def test_google_state_is_one_use_and_required(self):
        self.start()
        self.post("/connect/google/client", client_id="id", client_secret="sec")
        _, text = self.post("/connect/google/start", label="work")
        link = re.search(r'href="(https://accounts\.google\.com[^"]+)"', text).group(1).replace("&amp;", "&")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        self.assertEqual(q["redirect_uri"], [f"http://127.0.0.1:{self.port}/"])
        self.assertIn("calendar.events", q["scope"][0])
        state = q["state"][0]
        self.assertEqual(self.req("GET", "/?code=abc&state=forged", cookie=False)[0].status, 400)
        with mock.patch.object(calsync, "http", return_value={"refresh_token": "rt"}) as call:
            r, _ = self.req("GET", f"/?code=abc&state={state}", cookie=False, host=f"127.0.0.1:{self.port}")
        self.assertEqual(r.status, 303)
        self.assertTrue(r.getheader("Location").startswith(f"http://localhost:{self.port}/account?label=work"))
        self.assertEqual(call.call_args.kwargs["form"]["code"], "abc")
        self.assertEqual(calsync._accounts()["work"], {"provider": "google", "refresh_token": "rt"})
        self.assertEqual(self.req("GET", f"/?code=abc&state={state}", cookie=False)[0].status, 400)

    def test_google_paste_back(self):
        self.start()
        self.post("/connect/google/client", client_id="id", client_secret="sec")
        _, text = self.post("/connect/google/start", label="home")
        state = urllib.parse.parse_qs(re.search(r'href="(https://accounts[^"]+)"', text).group(1).replace("&amp;", "&").split("?")[1])["state"][0]
        with mock.patch.object(calsync, "http", return_value={"refresh_token": "rt"}):
            r, _ = self.post("/connect/google/paste", address=f"http://127.0.0.1:8484/?state={state}&code=xyz&scope=s")
        self.assertEqual(r.status, 303)
        self.assertIn("home", calsync._accounts())

    # --- syncing

    def two_calendars(self):
        calsync._save_account("one", {"provider": "ics", "url": "https://example.com/a.ics"})
        calsync.update_config(lambda c: c.update(calendars=[
            {"name": "a", "label": "a", "account": "one", "calendar_id": "x"},
            {"name": "b", "label": "b", "account": "one", "calendar_id": "y"}]))
        self.cals = {"a": Fake(), "b": Fake()}
        self.adapter = lambda cfg: self.cals[cfg["name"]]

    def wait_for_run(self):
        for _ in range(100):
            if not calsync_web.run_state["running"]:
                return
            time.sleep(0.05)
        self.fail("run never finished")

    def test_nothing_syncs_until_started_and_preview_changes_nothing(self):
        self.start()
        self.two_calendars()
        self.cals["a"].events["e"] = native("Dentist")
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))
        with mock.patch.object(calsync, "make_adapter", self.adapter):
            self.post("/preview")
            text = self.req("GET", "/")[1]
            self.assertIn("1 to add", text)
            self.assertIn("Dentist", text)
            self.assertEqual(self.cals["b"].events, {})
            self.assertNotIn("1 to add", self.req("GET", "/")[1])   # shown once
            self.post("/start")
            self.wait_for_run()
        self.assertTrue(calsync.load_json(calsync.CONFIG, {})["enabled"])
        self.assertEqual([e["title"] for e in self.cals["b"].events.values()], ["Dentist"])
        self.assertEqual(calsync.load_json(calsync.LASTRUN, {})["created"], 1)

    def test_calendar_options_and_purge_only_touch_copies(self):
        self.start()
        self.two_calendars()
        self.cals["a"].events["e"] = native("Mine")
        self.post("/calendar/save", name="b", busy_only="on", skip="a")
        b = next(c for c in calsync.load_json(calsync.CONFIG, {})["calendars"] if c["name"] == "b")
        self.assertEqual((b["busy_only"], b["skip_sources"]), (True, ["a"]))
        self.post("/calendar/save", name="b", busy_only="on")
        self.cals["b"].create({"title": "Busy", "start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z", "all_day": False, "busy": True, "location": ""}, "a|e", "a")
        self.cals["b"].events["n"] = native("Native on b")
        with mock.patch.object(calsync, "make_adapter", self.adapter):
            _, text = self.post("/purge", name="b")
            self.assertIn("Delete 1 copy from b", text)
            self.assertEqual(len(self.cals["b"].events), 2)
            _, text = self.post("/purge", name="b", confirm="1")
        self.assertEqual([e["title"] for e in self.cals["b"].events.values()], ["Native on b"])
        self.assertEqual([e["title"] for e in self.cals["a"].events.values()], ["Mine"])

    def test_bad_calendar_names_are_refused(self):
        self.start()
        calsync._save_account("one", {"provider": "ics", "url": "https://example.com/a.ics"})
        for bad in ("a|b", "../x", "", "a b"):
            self.post("/calendar/add", name=bad, account="one", calendar_id="ics")
        self.assertEqual(calsync.load_json(calsync.CONFIG, {"calendars": []})["calendars"], [])

    def test_ics_calendar_is_always_read_only(self):
        self.start()
        calsync._save_account("one", {"provider": "ics", "url": "https://example.com/a.ics"})
        self.post("/calendar/add", name="feed", account="one", calendar_id="ics")
        self.assertTrue(calsync.load_json(calsync.CONFIG, {})["calendars"][0]["read_only"])

    def test_settings_validate(self):
        self.start()
        self.assertIn("isn&#x27;t known", self.post("/settings/save", interval="5", timezone="Mars/Base")[1])
        self.assertIn("listed", self.post("/settings/save", interval="1", timezone="UTC")[1])
        self.post("/settings/save", interval="10", timezone="Europe/Paris")
        cfg = calsync.load_json(calsync.CONFIG, {})
        self.assertEqual((cfg["interval_minutes"], cfg["timezone"]), (10, "Europe/Paris"))

    def test_scheduler_runs_only_when_enabled(self):
        self.start()
        self.two_calendars()
        self.cals["a"].events["e"] = native("Dentist")
        stop = calsync_web.threading.Event()
        waits = iter([False, False, True])
        stop.wait = lambda t: next(waits)
        with mock.patch.object(calsync, "make_adapter", self.adapter):
            calsync_web.scheduler(stop)
            self.wait_for_run()
            self.assertEqual(self.cals["b"].events, {})
            calsync.update_config(lambda c: c.update(enabled=True))
            waits = iter([False, True])
            stop.wait = lambda t: next(waits)
            calsync_web.scheduler(stop)
            self.wait_for_run()
        self.assertEqual(len(self.cals["b"].events), 1)

    def test_a_strange_csrf_value_is_refused_with_a_reply(self):
        self.start()
        r, text = self.req("POST", "/run", {"csrf": "\u00e9\u00e9"})
        self.assertEqual(r.status, 400)
        self.assertIn("expired", text)

    def test_one_copy_is_not_called_copies(self):
        self.assertEqual(calsync_web.copies(1), "1 copy")
        self.assertEqual(calsync_web.copies(0), "0 copies")
        self.assertEqual(calsync_web.copies(2), "2 copies")

    def test_negative_length_is_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/run", skip_host=True)
        c.putheader("Host", f"localhost:{self.port}")
        c.putheader("Origin", f"http://localhost:{self.port}")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    def test_health_needs_nothing(self):
        self.assertEqual(self.req("GET", "/healthz", host="anything.example")[1], "ok")


    # --- sync rules, the why, activity, the Calendar tab's own events

    def test_a_rule_saves_per_direction_and_keeps_the_old_setting_in_step(self):
        self.start()
        self.two_calendars()
        self.assertIn("a <span class=\"arrow\">→</span> b", self.req("GET", "/pair?src=a&dst=b")[1])
        self.assertEqual(self.req("GET", "/pair?src=a&dst=a")[0].status, 404)
        r, _ = self.post("/pair/save", src="a", dst="b", copy_title="", title_text="Held <b>", tag="[A]", colour="red",
                         f_keywords="lunch, gym", f_keyword_mode="exclude", f_days=["0", "1"], f_from="09:00", copy_location="1")
        self.assertEqual(r.getheader("Location"), "/?m=rulesaved")
        cfg = calsync.load_json(calsync.CONFIG, {})
        rule = cfg["rules"]["a>b"]
        self.assertEqual((rule["enabled"], rule["copy"]["title"], rule["copy"]["title_text"], rule["tag"], rule["colour"]), (False, False, "Held <b>", "[A]", "red"))
        self.assertEqual((rule["filters"]["keywords"], rule["filters"]["days"], rule["filters"]["from"]), (["lunch", "gym"], [0, 1], "09:00"))
        self.assertEqual(next(c for c in cfg["calendars"] if c["name"] == "b")["skip_sources"], ["a"])     # off, so the old setting says skip too
        self.post("/pair/save", src="a", dst="b", enabled="1", copy_title="1")
        self.assertEqual(next(c for c in calsync.load_json(calsync.CONFIG, {})["calendars"] if c["name"] == "b")["skip_sources"], [])
        page = self.req("GET", "/")[1]
        self.assertIn("What goes where", page)
        self.assertIn('href="/pair?src=a&amp;dst=b"', page)

    def test_the_preview_says_why(self):
        self.start()
        self.two_calendars()
        self.cals["a"].events["e"] = native("Team lunch <i>")
        self.cals["a"].events["f"] = native("Standup")
        calsync.update_config(lambda c: c.update(rules={"a>b": {"filters": {"keywords": ["lunch"]}}}))
        with mock.patch.object(calsync, "make_adapter", self.adapter):
            self.post("/preview")
            text = self.req("GET", "/")[1]
        self.assertIn("Why each event is or isn", text)
        self.assertIn("Team lunch &lt;i&gt;", text)
        self.assertIn("its title has &quot;lunch&quot;", text)
        self.assertIn("copied as &quot;Standup&quot;", text)

    def test_resync_and_activity(self):
        self.start()
        self.two_calendars()
        with mock.patch.object(calsync_web, "run_in_background", return_value=True) as run:
            self.assertEqual(self.post("/resync", pair="a>b")[0].getheader("Location"), "/?m=resyncing")
            run.assert_called_once_with(pair="a>b", refresh=True, trigger="resync")
            self.post("/resync", pair="a>nope")
            self.assertEqual(run.call_count, 1)
        calsync.record_activity({"at": "2030-01-01T10:00:00Z", "created": 1, "trigger": "manual", "lines": ["+ b: <script>x</script> 2030"]})
        text = self.req("GET", "/activity")[1]
        self.assertIn("1 added", text)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", text)

    def test_stopping_a_calendar_asks_what_happens_to_its_copies(self):
        self.start()
        self.two_calendars()
        self.assertIn("Keep them", self.post("/calendar/remove", name="a")[1])
        self.assertEqual(len(calsync.load_json(calsync.CONFIG, {})["calendars"]), 2)
        self.post("/calendar/remove", name="a", confirm="1", copies="keep")
        cfg = calsync.load_json(calsync.CONFIG, {})
        self.assertEqual(([c["name"] for c in cfg["calendars"]], cfg["kept_sources"]), (["b"], ["a"]))

    def test_pages_use_one_same_origin_script_and_still_work_without_it(self):
        self.start()
        r, text = self.req("GET", "/calendar")
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(re.findall(r"<script[^>]*>", text), ['<script src="/app.js" defer>'])
        self.assertNotIn("style=", text)
        r, js = self.req("GET", "/app.js")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/javascript"))
        self.assertIn("pointerdown", js)

    def test_new_event_change_and_undo(self):
        self.start()
        self.two_calendars()
        with mock.patch.object(calsync, "event_create", return_value="new1") as create, \
             mock.patch.object(calsync, "event_delete") as delete, mock.patch.object(calsync, "event_update") as update:
            r, _ = self.post("/event/create", cal="a", title="Run", date="2030-01-02", start="07:00", end="08:00", repeat="weekdays",
                             until="2030-01-31", busy="1", notify="1", back="/calendar?view=week&d=2030-01-02")
            loc = r.getheader("Location")
            self.assertTrue(loc.startswith("/calendar?view=week&d=2030-01-02&m=added_ev&undo="))
            name, raw = create.call_args.args
            self.assertEqual((name, raw["title"], raw["repeat"][:30], raw["start"].hour), ("a", "Run", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,", 7))
            token = loc.split("undo=")[1]
            self.assertIn("Undo", self.req("GET", loc)[1])
            self.post("/undo", token=token, back="/calendar")
            delete.assert_called_once_with("a", "new1", "all", False)       # undoing a new event removes it
            # a change waits, so Undo just drops it
            r, _ = self.post("/event/save", cal="a", uid="u1", title="Moved", date="2030-01-03", start="09:00", end="10:00", scope="all", back="/calendar")
            token = r.getheader("Location").split("undo=")[1]
            self.assertEqual(calsync_web.pending_ops[token]["scope"], "all")
            self.post("/undo", token=token, back="/calendar")
            self.assertNotIn(token, calsync_web.pending_ops)
            # a drag answers with where to go next
            r, text = self.post("/event/move", cal="a", uid="u2", date="2030-01-03", start="11:00", end="11:30", back="/calendar?view=day")
            self.assertTrue(json.loads(text)["go"].startswith("/calendar?view=day&m=pending&undo="))
            op = calsync_web.pending_ops[json.loads(text)["go"].split("undo=")[1]]
            op["due"] = 0
            calsync_web.run_op(op)
            name, uid, changes, scope, notify, href = update.call_args.args
            self.assertEqual((name, uid, scope, changes["start"].hour, changes["end"].minute), ("a", "u2", "this", 11, 30))

    def test_back_links_only_go_to_the_calendar(self):
        self.start()
        with mock.patch.object(calsync, "event_delete"):
            r, _ = self.post("/event/delete", cal="a", uid="x", back="https://evil.example/")
        self.assertTrue(r.getheader("Location").startswith("/calendar?m=pending"))

    def test_view_toggles_are_remembered(self):
        self.start()
        self.post("/calendar/prefs", pref="hide_weekends", back="/calendar")
        self.assertTrue(calsync.load_json(calsync.CONFIG, {})["view"]["hide_weekends"])
        self.post("/calendar/prefs", pref="bogus", back="/calendar")
        self.assertNotIn("bogus", calsync.load_json(calsync.CONFIG, {})["view"])

    def test_push_hooks_need_their_secret(self):
        self.assertEqual(self.req("POST", "/hook/microsoft?validationToken=abc%3Cx", form={}, cookie=False)[1], "abc<x")
        self.assertEqual(self.req("POST", "/hook/google", form={}, cookie=False)[0].status, 404)       # never set up
        calsync.ensure_home()
        calsync.save_json(calsync_web.push_file(), {"secret": "s3cret"})
        calsync_web.pushed.clear()
        self.assertEqual(self.req("POST", "/hook/google", form={}, headers={"X-Goog-Channel-Token": "nope"}, cookie=False)[0].status, 403)
        self.assertFalse(calsync_web.pushed.is_set())
        self.assertEqual(self.req("POST", "/hook/google", form={}, headers={"X-Goog-Channel-Token": "s3cret"}, cookie=False)[0].status, 200)
        self.assertTrue(calsync_web.pushed.is_set())

    def test_search_page(self):
        self.start()
        with mock.patch.object(calsync, "search_events", return_value=([dict(native("Dentist <b>"), calendar="a")], ["b: offline"])) as search:
            text = self.req("GET", "/search?q=dentist+cal%3Aa")[1]
        search.assert_called_once()
        self.assertIn("Dentist &lt;b&gt;", text)
        self.assertIn("b: offline", text)


class HostileConnectionTests(unittest.TestCase):
    def serve(self, **attrs):
        handler = type("H", (calsync_web.Handler,), attrs.pop("handler", {}))
        server = type("S", (calsync_web.Server,), attrs)(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def connect(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def test_a_connection_that_goes_quiet_is_dropped(self):
        self.assertTrue(0 < calsync_web.Handler.timeout <= 60)     # the real page has a limit; the rest shortens it to test it
        for sent in (b"", b"GET / HT", b"POST /run HTTP/1.1\r\nHost: localhost:PORT\r\nOrigin: http://localhost:PORT\r\nContent-Length: 100\r\n\r\n"):
            port = self.serve(handler={"timeout": 0.2})
            sock = self.connect(port)
            sock.sendall(sent.replace(b"PORT", str(port).encode()))
            self.assertEqual(sock.recv(100), b"", sent)     # closed by the server, not this test's 5 second timeout

    def test_connections_past_the_cap_are_turned_away_and_the_page_recovers(self):
        port = self.serve(MAX_CONNECTIONS=2)
        held = [self.connect(port), self.connect(port)]
        time.sleep(.1)
        sock = self.connect(port)
        sock.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        try:
            self.assertEqual(sock.recv(100), b"")
        except ConnectionResetError:
            pass
        for s in held:
            s.close()
        for _ in range(50):
            time.sleep(.05)
            sock = self.connect(port)
            sock.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
            try:
                if sock.recv(100).startswith(b"HTTP/1.0 200"):
                    return
            except ConnectionResetError:
                pass
        self.fail("the page never came back")


if __name__ == "__main__":
    unittest.main()
