"""One-click sign-in: CalSync's own Google and Microsoft apps, PKCE and a loopback redirect: python3 -m unittest test_oauth"""
import http.client, json, os, re, tempfile, threading, unittest, urllib.parse, urllib.request
from unittest import mock

home = os.environ.setdefault("CALSYNC_HOME", tempfile.mkdtemp())    # never your real one
os.environ["CALSYNC_TZ"] = "America/New_York"
import calsync, calsync_web

TEST_APPS = {"CALSYNC_GOOGLE_CLIENT_ID": "test-id.apps.googleusercontent.com", "CALSYNC_GOOGLE_CLIENT_SECRET": "test-installed-secret",
             "CALSYNC_MS_CLIENT_ID": "00000000-test-0000-0000-000000000000"}


def clear_accounts():
    calsync.ensure_home()
    calsync.save_json(os.path.join(calsync.SECRETS, "accounts.json"), {})
    for f in ("google-client.json", "microsoft-client.json"):
        try:
            os.remove(os.path.join(calsync.SECRETS, f))
        except OSError:
            pass


class TokenServer:
    """Stands in for Google's and Microsoft's token endpoints: records each form, answers with a refresh token."""
    def __init__(self, reply=None, error=None):
        self.forms, self.reply, self.error = [], reply or {"access_token": "a", "refresh_token": "r-123"}, error

    def __call__(self, method, url, token=None, body=None, form=None, headers=None):
        self.forms.append((url, dict(form or {})))
        if self.error:
            raise RuntimeError(f"POST {url} -> 400 {self.error}")
        return self.reply


@mock.patch.dict(os.environ, TEST_APPS)
class EngineTests(unittest.TestCase):
    def setUp(self):
        clear_accounts()

    def test_pkce_matches_the_rfc_example(self):
        self.assertEqual(calsync.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        v, c = calsync.pkce()
        self.assertTrue(43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", v))
        self.assertEqual(c, calsync.pkce_challenge(v))
        self.assertNotEqual(v, calsync.pkce()[0])

    def test_no_built_in_app_until_one_is_set(self):
        with mock.patch.dict(os.environ, {"CALSYNC_GOOGLE_CLIENT_ID": "", "CALSYNC_MS_CLIENT_ID": ""}), \
             mock.patch.object(calsync, "CALSYNC_GOOGLE_ID", "REPLACE-ME"), mock.patch.object(calsync, "CALSYNC_MICROSOFT_ID", "REPLACE-ME"):
            self.assertIsNone(calsync.builtin_app("google"))
            self.assertIsNone(calsync.builtin_app("microsoft"))
            with self.assertRaises(RuntimeError):
                calsync.oauth_start("google", "http://127.0.0.1:1/oauth/google")
        self.assertEqual(calsync.builtin_app("microsoft"), {"client_id": TEST_APPS["CALSYNC_MS_CLIENT_ID"]})      # a public client: no secret

    def test_sign_in_addresses_ask_for_little_and_carry_the_challenge(self):
        g = calsync.oauth_start("google", "http://127.0.0.1:8484/oauth/google")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(g["url"]).query)
        self.assertTrue(g["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual((q["code_challenge"][0], q["code_challenge_method"][0]), (calsync.pkce_challenge(g["verifier"]), "S256"))
        self.assertEqual(set(q["scope"][0].split()), {"https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist.readonly"})
        self.assertEqual((q["state"][0], q["redirect_uri"][0], q["access_type"][0]), (g["state"], "http://127.0.0.1:8484/oauth/google", "offline"))
        self.assertNotIn("client_secret", q)
        self.assertNotIn(g["verifier"], g["url"])
        m = calsync.oauth_start("microsoft", calsync.loopback_redirect("microsoft", 8495))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(m["url"]).query)
        self.assertEqual((q["redirect_uri"][0], q["scope"][0]), ("http://localhost:8495", "offline_access Calendars.ReadWrite"))
        self.assertTrue(m["url"].startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"))

    def test_finishing_sends_the_verifier_and_stores_a_built_in_account(self):
        tokens = TokenServer()
        with mock.patch.object(calsync, "http", tokens):
            label = calsync.oauth_finish("google", "the-code", "the-verifier", "http://127.0.0.1:8484/oauth/google")
            again = calsync.oauth_finish("google", "c2", "v2", "http://127.0.0.1:8484/oauth/google")
        url, form = tokens.forms[0]
        self.assertEqual(url, "https://oauth2.googleapis.com/token")
        self.assertEqual((form["code_verifier"], form["code"], form["grant_type"]), ("the-verifier", "the-code", "authorization_code"))
        self.assertEqual((label, again), ("google", "google-2"))
        self.assertEqual(calsync._accounts()["google"], {"provider": "google", "app": "calsync", "refresh_token": "r-123"})

    def test_microsoft_sends_no_secret(self):
        tokens = TokenServer()
        with mock.patch.object(calsync, "http", tokens):
            label = calsync.oauth_finish("microsoft", "c", "v", "http://localhost:8495")
        self.assertNotIn("client_secret", tokens.forms[0][1])
        self.assertEqual(calsync._accounts()[label]["provider"], "outlook")

    def test_refresh_uses_the_built_in_app_for_built_in_accounts_only(self):
        calsync._save_account("mine", {"provider": "google", "app": "calsync", "refresh_token": "r1"})
        calsync._save_account("own", {"provider": "google", "refresh_token": "r2"})
        calsync.save_json(os.path.join(calsync.SECRETS, "google-client.json"), {"client_id": "own-id", "client_secret": "own-secret"})
        tokens = TokenServer()
        with mock.patch.object(calsync, "http", tokens):
            calsync.google_access_token("mine")
            calsync.google_access_token("own")
        self.assertEqual([f["client_id"] for _, f in tokens.forms], [TEST_APPS["CALSYNC_GOOGLE_CLIENT_ID"], "own-id"])
        calsync._save_account("ms", {"provider": "outlook", "app": "calsync", "refresh_token": "r3", "tenant": "common"})
        with mock.patch.object(calsync, "http", TokenServer()) as t:
            calsync.microsoft_access_token("ms")
        self.assertEqual(t.forms[0][1]["client_id"], TEST_APPS["CALSYNC_MS_CLIENT_ID"])
        self.assertNotIn("client_secret", t.forms[0][1])
        # CalSync Hosted's web app: its secret is in the client file and goes only with its own ("calsync-web") tokens
        calsync.save_json(os.path.join(calsync.SECRETS, "microsoft-client.json"), {"client_id": "web-id", "client_secret": "web-secret"})
        calsync._save_account("hosted", {"provider": "outlook", "app": "calsync-web", "refresh_token": "r4"})
        with mock.patch.object(calsync, "http", TokenServer()) as t:
            calsync.microsoft_access_token("hosted")
            calsync.microsoft_access_token("ms")
        self.assertEqual([(f["client_id"], f.get("client_secret")) for _, f in t.forms],
                         [("web-id", "web-secret"), (TEST_APPS["CALSYNC_MS_CLIENT_ID"], None)])

    def test_the_calsync_microsoft_app_is_built_in(self):
        with mock.patch.dict(os.environ, {"CALSYNC_MS_CLIENT_ID": ""}):
            self.assertEqual(calsync.builtin_app("microsoft"), {"client_id": "a2275ea1-af64-4bbb-b10b-a5841ac41268"})

    def test_the_calsync_google_app_is_built_in(self):
        with mock.patch.dict(os.environ, {"CALSYNC_GOOGLE_CLIENT_ID": "", "CALSYNC_GOOGLE_CLIENT_SECRET": ""}):
            app = calsync.builtin_app("google")
        self.assertEqual(app["client_id"], "463893300981-jhvspr3td5p3vj04a6djgr43f7rfgs4g.apps.googleusercontent.com")
        self.assertTrue(app["client_secret"].startswith("GOCSPX-"))

    def test_an_organisation_that_needs_an_admin_is_named_plainly(self):
        with self.assertRaises(calsync.SignInBlocked):
            calsync.oauth_refused("microsoft", {"error": "access_denied", "error_description": "AADSTS90094: Admin consent is required for this app."})
        with self.assertRaises(calsync.SignInBlocked):
            calsync.oauth_refused("microsoft", {"error": "consent_required"})
        self.assertIn("didn't allow", calsync.oauth_refused("google", {"error": "access_denied"}))
        with mock.patch.object(calsync, "http", TokenServer(error='{"error":"invalid_grant","error_description":"AADSTS65001: The user or administrator has not consented"}')):
            with self.assertRaises(calsync.SignInBlocked):
                calsync.oauth_finish("microsoft", "c", "v", "http://localhost:1")

    def test_command_line_listens_on_a_random_loopback_port(self):
        seen = {}

        def browser(url):       # what the person's browser does: Google sends it back to the redirect with a code
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            seen["redirect"] = q["redirect_uri"][0]
            back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"state": q["state"][0], "code": "abc"})
            threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()
        tokens = TokenServer()
        with mock.patch.object(calsync, "http", tokens):
            label = calsync.loopback_signin("google", "work", ready=browser, timeout=10)
        self.assertEqual(label, "work")
        self.assertRegex(seen["redirect"], r"^http://127\.0\.0\.1:\d+/oauth/google$")
        self.assertNotEqual(seen["redirect"], "http://127.0.0.1:8484/oauth/google")
        self.assertEqual(tokens.forms[0][1]["redirect_uri"], seen["redirect"])

    def test_command_line_ignores_an_answer_for_another_request(self):
        def browser(url):
            back = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0] + "?state=forged&code=evil"
            threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()
        with mock.patch.object(calsync, "http", TokenServer()) as tokens, self.assertRaises(RuntimeError):
            calsync.loopback_signin("google", ready=browser, timeout=10)
        self.assertEqual(calsync._accounts(), {})


@mock.patch.dict(os.environ, TEST_APPS)
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
        for root, _, files in os.walk(home):
            for f in files:
                os.remove(os.path.join(root, f))
        clear_accounts()
        calsync_web.failed_logins.clear()
        calsync_web.pending_oauth.clear()
        self.cookie = None

    def req(self, path, host=None, cookie=True, method="GET", form=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"localhost:{self.port}"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
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
        self.req("/first-run", method="POST", form={"password": "correct horse battery"})

    def links(self, host=None):
        text = self.req("/", host=host)[1]
        found = {}
        for href, wait in re.findall(r'<a class="button" href="([^"]+)" target="_blank" rel="noopener" data-oauth-wait="([^"]+)">Sign in with', text):
            found["google" if "accounts.google.com" in href else "microsoft"] = (href.replace("&amp;", "&"), wait.replace("&amp;", "&"))
        return found, text

    def test_connect_is_one_button_each_with_the_old_way_under_advanced(self):
        self.start()
        found, text = self.links()
        self.assertEqual(set(found), {"google", "microsoft"})
        self.assertIn('href="/connect/advanced"', text)
        self.assertIn("Apple iCloud", text)          # app passwords, as before
        self.assertIn("/connect/ics", text)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(found["google"][0]).query)
        self.assertEqual(q["redirect_uri"][0], f"http://127.0.0.1:{self.port}/oauth/google")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(found["microsoft"][0]).query)
        self.assertEqual(q["redirect_uri"][0], f"http://localhost:{self.port}")
        adv = self.req("/connect/advanced")[1]
        self.assertIn('href="/connect/google"', adv)
        self.assertIn("console.cloud.google.com", self.req("/connect/google")[1])     # the own-app steps are still there

    def test_opened_from_another_machine_there_is_no_one_click(self):
        self.start()
        found, text = self.links(host=f"192.168.1.20:{self.port}")
        self.assertEqual(found, {})
        self.assertIn("on the computer it runs on", text)
        with mock.patch.object(calsync_web, "PUBLIC_URL", "https://cal.example.com"):
            self.assertEqual(self.links()[0], {})

    def test_sign_in_comes_back_and_moves_the_waiting_page_on(self):
        self.start()
        found, _ = self.links()
        url, wait = found["google"]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        waiting = self.req(wait)[1]
        self.assertIn("moves on by itself", waiting)
        self.assertIn("verified this app", waiting)   # until Google's review is done
        self.assertNotIn("verified this app", self.req(found["microsoft"][1])[1])
        tokens = TokenServer()
        with mock.patch.object(calsync, "http", tokens):
            # the system browser has no CalSync session (the desktop app's case): it gets a "go back" page
            r, text = self.req(f"/oauth/google?state={state}&code=c0de", host=f"127.0.0.1:{self.port}", cookie=False)
        self.assertIn("go back to CalSync", text)
        verifier = tokens.forms[0][1]["code_verifier"]
        self.assertEqual(calsync.pkce_challenge(verifier), urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["code_challenge"][0])
        self.assertEqual(calsync._accounts()["google"]["app"], "calsync")
        r, _ = self.req(wait)
        self.assertEqual((r.status, r.getheader("Location")), (303, "/account?label=google"))
        # a state is good once
        r, _ = self.req(f"/oauth/google?state={state}&code=again", cookie=False)
        self.assertEqual(r.status, 400)

    def test_a_signed_in_browser_goes_straight_to_choosing_calendars(self):
        self.start()
        url = self.links()[0]["microsoft"][0]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        with mock.patch.object(calsync, "http", TokenServer()):
            r, _ = self.req(f"/?state={state}&code=c")
        self.assertEqual(r.getheader("Location"), "/account?label=microsoft")

    def test_wrong_provider_or_unknown_state_is_refused(self):
        self.start()
        url = self.links()[0]["google"][0]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        with mock.patch.object(calsync, "http", TokenServer()) as tokens:
            self.assertEqual(self.req(f"/oauth/microsoft?state={state}&code=c", cookie=False)[0].status, 400)
            self.assertEqual(self.req("/oauth/google?state=made-up&code=c", cookie=False)[0].status, 400)
        self.assertEqual(calsync._accounts(), {})

    def test_a_school_that_needs_admin_approval_is_offered_the_ics_link(self):
        self.start()
        url, wait = self.links()[0]["microsoft"]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        desc = urllib.parse.quote("AADSTS90094: An administrator of Indiana University has set a policy that prevents you from granting CalSync the permissions it is requesting.")
        _, text = self.req(f"/?state={state}&error=access_denied&error_description={desc}", cookie=False)
        self.assertIn("needs an administrator to approve CalSync", text)
        self.assertIn('href="/connect/ics"', text)
        self.assertIn("needs an administrator", self.req(wait)[1])
        self.assertEqual(calsync._accounts(), {})

    def test_flows_are_capped(self):
        self.start()
        for _ in range(calsync_web.MAX_OAUTH_FLOWS):
            self.links()
        self.assertLessEqual(len(calsync_web.pending_oauth), calsync_web.MAX_OAUTH_FLOWS)


if __name__ == "__main__":
    unittest.main()
