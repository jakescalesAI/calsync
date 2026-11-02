"""Web page tests against a real local server and in-memory calendars: python3 -m unittest test_web"""
import http.client, json, os, re, stat, tempfile, threading, time, unittest, urllib.parse
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
        calsync_web.failed_logins.clear()
        calsync_web.pending_google.clear()
        self.cookie = None

    def req(self, method, path, form=None, host=None, origin=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"localhost:{self.port}"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if origin:
            h["Origin"] = origin
        if cookie and self.cookie:
            h["Cookie"] = "calsync_session=" + self.cookie
        c.request(method, path, body, h)
        r = c.getresponse()
        text = r.read().decode()
        got = r.getheader("Set-Cookie")
        if got and cookie:
            self.cookie = got.split(";")[0].split("=", 1)[1] or None
        c.close()
        return r, text

    def start(self):
        r, text = self.req("POST", "/first-run", {"password": "correct horse battery"})
        self.assertEqual(r.status, 303)

    def csrf(self, path="/settings"):
        return re.search(r'name="csrf" value="(\w+)"', self.req("GET", path)[1]).group(1)

    def post(self, path, **form):
        return self.req("POST", path, dict(form, csrf=self.csrf()))

    # --- who gets in

    def test_first_run_sets_password_and_files_are_private(self):
        r, text = self.req("GET", "/")
        self.assertIn("Choose a password", text)
        self.assertNotIn(calsync_web.setup_code, text)
        self.start()
        self.assertIn("Nothing is connected", self.req("GET", "/")[1])
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(calsync_web.web_file()).st_mode), 0o600)
        self.assertNotIn("correct horse", open(calsync_web.web_file()).read())

    def test_first_run_can_only_happen_once(self):
        self.start()
        self.cookie = None
        self.req("POST", "/first-run", {"password": "another password here"})
        self.assertIsNone(self.cookie)
        self.assertEqual(self.req("POST", "/login", {"password": "another password here"})[0].status, 200)
        self.assertIsNone(self.cookie)

    def test_remote_first_run_needs_the_code(self):
        with mock.patch.object(calsync_web, "is_loopback", return_value=False):
            self.assertIn("Setup code", self.req("GET", "/")[1])
            self.assertIn("isn&#x27;t right", self.req("POST", "/first-run", {"password": "long enough password", "code": "nope"})[1])
            self.assertIsNone(calsync_web.web_config())
            self.assertEqual(self.req("POST", "/first-run", {"password": "long enough password", "code": calsync_web.setup_code})[0].status, 303)

    def test_every_page_and_action_needs_signing_in(self):
        self.start()
        self.cookie = None
        for path in ("/connect", "/connect/google", "/connect/ics", "/settings", "/account?label=x"):
            self.assertIn("Sign in", self.req("GET", path)[1], path)
        for path in ("/run", "/start", "/preview", "/purge", "/connect/google/client", "/settings/password", "/calendar/add"):
            r, _ = self.req("POST", path, {"csrf": "x"})
            self.assertEqual((r.status, r.getheader("Location")), (303, "/"), path)
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))

    def test_forged_and_expired_sessions_fail(self):
        self.start()
        good = self.cookie
        cfg = calsync_web.web_config()
        self.assertFalse(calsync_web.session_ok(cfg, "9999999999." + "0" * 64))
        self.assertFalse(calsync_web.session_ok(cfg, good.split(".")[0] + "." + "a" * 64))
        old = "1000"
        self.assertFalse(calsync_web.session_ok(cfg, f"{old}.{calsync_web.sign(cfg['key'], 'session.' + old)}"))

    def test_wrong_passwords_get_slowed_down(self):
        self.start()
        self.cookie = None
        for _ in range(calsync_web.ATTEMPTS_PER_MINUTE):
            self.assertEqual(self.req("POST", "/login", {"password": "nope"})[0].status, 200)
        self.assertEqual(self.req("POST", "/login", {"password": "correct horse battery"})[0].status, 429)

    def test_csrf_origin_and_host_are_checked(self):
        self.start()
        self.assertEqual(self.req("POST", "/start", {"csrf": "wrong"})[0].status, 400)
        self.assertFalse(calsync.load_json(calsync.CONFIG, {}).get("enabled"))
        token = self.csrf()
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, origin="https://evil.example")[0].status, 400)
        self.assertEqual(self.req("GET", "/", host="evil.example")[0].status, 400)
        self.assertEqual(self.req("POST", "/start", {"csrf": token}, host="evil.example")[0].status, 400)
        self.assertEqual(self.req("GET", "/", host="192.168.1.20:8484")[0].status, 200)

    def test_security_headers_and_no_scripts(self):
        r, text = self.req("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertNotIn("<script", text)
        self.assertNotIn("<style", text)

    def test_cookie_flags(self):
        r, _ = self.req("POST", "/first-run", {"password": "correct horse battery"})
        flags = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", flags)
        self.assertIn("SameSite=Lax", flags)

    # --- secrets stay put

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
            self.assertIn("Delete 1 copies", text)
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

    def test_changing_password_signs_others_out(self):
        self.start()
        other = self.cookie
        self.assertIn("isn&#x27;t right", self.post("/settings/password", old="wrong", new="new password long")[1])
        self.post("/settings/password", old="correct horse battery", new="new password long")
        self.assertFalse(calsync_web.session_ok(calsync_web.web_config(), other))
        self.assertTrue(calsync_web.session_ok(calsync_web.web_config(), self.cookie))

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

    def test_health_needs_nothing(self):
        self.assertEqual(self.req("GET", "/healthz", host="anything.example")[1], "ok")


if __name__ == "__main__":
    unittest.main()
