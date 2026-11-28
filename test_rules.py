"""Sync rules, filters, the Preview's why, keeping copies, Resync, the activity log, and the Calendar tab's writes:
python3 -m unittest test_rules"""
import datetime as dt, json, os, tempfile, unittest
from unittest import mock

os.environ.setdefault("CALSYNC_HOME", tempfile.mkdtemp())    # never your real one
os.environ["CALSYNC_TZ"] = "America/New_York"
import calsync
from test_calsync import FakeCal, NOW


def run(cals, cfg=None, **kw):
    cfgs = [dict({"name": n}, **(cfg or {}).get(n, {})) for n in cals]
    whole = {"calendars": cfgs, **(cfg or {}).get("*", {})}
    return calsync.sync(cfgs, cals, now=NOW, log=lambda *_: None, cfg=whole, **kw)


def rules(**pairs):
    """rules(a_b={...}) -> {"*": {"rules": {"a>b": {...}}}}"""
    return {"*": {"rules": {k.replace("_", ">"): v for k, v in pairs.items()}}}


T1, T2 = "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z"      # Mon Nov 2, 10-11 AM in New York


class LegacyConfigTests(unittest.TestCase):
    """A config from before rules keeps doing exactly what it did."""

    def test_rule_from_old_settings(self):
        cfg = {"calendars": [{"name": "a"}, {"name": "b", "busy_only": True, "skip_sources": ["c"]}, {"name": "c"}]}
        r = calsync.rule_for("a", "b", cfg)
        self.assertTrue(r["enabled"])
        self.assertEqual((r["copy"]["title"], r["copy"]["location"]), (False, False))
        self.assertFalse(calsync.rule_for("c", "b", cfg)["enabled"])
        self.assertTrue(calsync.rule_for("b", "a", cfg)["copy"]["title"])

    def test_render_is_unchanged_for_old_configs(self):
        ev = {"uid": "1", "title": "Class", "start": T1, "end": T2, "all_day": False, "busy": True, "location": "Room 2", "private": False}
        f = calsync.render(ev, "a", {"name": "b"})
        self.assertEqual({k: f[k] for k in calsync.CORE}, {"title": "Class", "start": T1, "end": T2, "all_day": False, "busy": True, "location": "Room 2"})
        self.assertNotIn("extra", f)
        f = calsync.render(ev, "a", {"name": "b", "busy_only": True})
        self.assertEqual((f["title"], f["location"], f["label"]), ("Busy", "", "another calendar"))

    def test_copies_made_before_rules_are_not_rewritten(self):
        a, b = FakeCal(), FakeCal()
        a.add("Class", T1, T2, location="Room 2")
        run({"a": a, "b": b})
        for e in b.copies():          # as a copy written by the old version reads back: no extra, no notes
            e.pop("extra", None)
        writes = b.writes
        s = run({"a": a, "b": b})
        self.assertEqual((s["created"], s["updated"], s["deleted"]), (0, 0, 0))
        self.assertEqual(b.writes, writes)


class PerDirectionTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeCal(), FakeCal()
        self.cals = {"a": self.a, "b": self.b}

    def test_each_direction_has_its_own_rule(self):
        self.a.add("Standup", T1, T2, location="Room 1")
        self.b.add("Doctor", T1, T2, location="Clinic")
        run(self.cals, rules(b_a={"copy": {"title": False, "location": False}}))
        self.assertEqual([(e["title"], e["location"]) for e in self.b.copies()], [("Standup", "Room 1")])
        self.assertEqual([(e["title"], e["location"]) for e in self.a.copies()], [("Busy", "")])

    def test_one_way(self):
        self.a.add("Standup", T1, T2)
        self.b.add("Doctor", T1, T2)
        run(self.cals, rules(b_a={"enabled": False}))
        self.assertEqual(len(self.b.copies()), 1)
        self.assertEqual(self.a.copies(), [])

    def test_info_to_copy_and_a_custom_title(self):
        self.a.add("Planning", T1, T2, description="Agenda: Q1", conference="https://meet.google.com/abc", attendees=["sam@x.com"])
        run(self.cals, rules(a_b={"copy": {"title": False, "title_text": "Held", "description": True, "conference": True, "attendees": True,
                                           "private": True, "reminders": True}}))
        c = self.b.copies()[0]
        self.assertEqual(c["title"], "Held")
        self.assertEqual(c["notes"], "Agenda: Q1\n\nJoin: https://meet.google.com/abc\n\nGuests: sam@x.com")
        self.assertTrue(c["reminders"] and c["extra"])

    def test_a_private_event_never_shares_its_details(self):
        self.a.add("Therapy", T1, T2, private=True, location="Clinic", description="notes", conference="https://zoom.us/j/1")
        run(self.cals, rules(a_b={"copy": {"description": True, "conference": True}}))
        c = self.b.copies()[0]
        self.assertEqual((c["title"], c["location"]), ("Busy", ""))
        self.assertNotIn("notes", c)

    def test_tag_and_colour(self):
        self.a.add("Standup", T1, T2, colours=["red"])
        run(self.cals, rules(a_b={"tag": "[Work]", "colour": "match"}))
        c = self.b.copies()[0]
        self.assertEqual((c["title"], c["colour"]), ("[Work] Standup", "red"))
        run(self.cals, rules(a_b={"tag": "[Work]", "colour": "teal"}))
        self.assertEqual(self.b.copies()[0]["colour"], "teal")

    def test_changing_what_is_copied_updates_the_copy_once(self):
        self.a.add("Planning", T1, T2, description="Agenda")
        run(self.cals)
        s = run(self.cals, rules(a_b={"copy": {"description": True}}))
        self.assertEqual(s["updated"], 1)
        self.assertEqual(self.b.copies()[0]["notes"], "Agenda")
        s = run(self.cals, rules(a_b={"copy": {"description": True}}))
        self.assertEqual((s["created"], s["updated"], s["deleted"]), (0, 0, 0))

    def test_the_copy_note_still_starts_every_description(self):
        f = {"title": "T", "start": T1, "end": T2, "all_day": False, "busy": True, "location": "", "notes": "Agenda", "extra": "x"}
        body = calsync.GoogleCalendar("acct", "cal")._body(f, "a|1", "Work")
        self.assertTrue(calsync.COPY_NOTE.match(body["description"]))
        self.assertTrue(body["description"].endswith("\n\nAgenda"))
        self.assertEqual(body["extendedProperties"]["private"][calsync.TAG_X], "x")
        back = calsync.GoogleCalendar._norm(dict(body, id="n", status="confirmed"))
        self.assertEqual(back["extra"], "x")
        o = calsync.OutlookCalendar("acct", "cal")._body(dict(f, private=True, colour="red"), "a|1", "Work")
        self.assertEqual((o["sensitivity"], o["categories"]), ("private", ["Red"]))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeCal(), FakeCal()
        self.cals = {"a": self.a, "b": self.b}

    def copied(self, filters, **ev):
        self.a.events.clear()
        self.b.events.clear()
        self.a.add(ev.pop("title", "Event"), ev.pop("start", T1), ev.pop("end", T2), **ev)
        run(self.cals, rules(a_b={"filters": filters}))
        return bool(self.b.copies())

    def test_each_filter(self):
        self.assertFalse(self.copied({"free": True}, busy=False))
        self.assertTrue(self.copied({"free": True}))
        self.assertFalse(self.copied({"tentative": True}, rsvp="maybe"))
        self.assertTrue(self.copied({"tentative": True}, rsvp="yes"))
        self.assertFalse(self.copied({"unanswered": True}, rsvp="none"))
        self.assertFalse(self.copied({"all_day": True}, start="2026-11-05", end="2026-11-06"))
        self.assertFalse(self.copied({}, kind="birthday"))                 # birthdays stay out unless you ask
        self.assertTrue(self.copied({"birthdays": False}, kind="birthday"))
        self.assertFalse(self.copied({"ooo": True}, kind="ooo"))
        self.assertFalse(self.copied({"colours": ["red"]}, colours=["red"]))
        self.assertTrue(self.copied({"colours": ["red"]}, colours=["blue"]))
        self.assertFalse(self.copied({"colours": ["red"], "colour_mode": "only"}, colours=["blue"]))
        self.assertFalse(self.copied({"keywords": ["lunch"]}, title="Team Lunch"))
        self.assertTrue(self.copied({"keywords": ["lunch", "gym"], "keyword_mode": "only"}, title="Gym"))
        self.assertFalse(self.copied({"keywords": ["gym"], "keyword_mode": "only"}, title="Dentist"))

    def test_days_and_hours_in_local_time(self):
        self.assertTrue(self.copied({"days": [0]}))                      # Monday in New York
        self.assertFalse(self.copied({"days": [1, 2, 3, 4]}))
        self.assertTrue(self.copied({"from": "09:00", "until": "17:00"}))
        self.assertFalse(self.copied({"from": "11:00"}))                 # starts at 10:00 there
        self.assertFalse(self.copied({"until": "10:00"}))
        self.assertTrue(self.copied({"from": "11:00"}, start="2026-11-05", end="2026-11-06"))     # all day skips the hours check

    def test_a_copy_goes_when_a_filter_starts_leaving_its_event_out(self):
        self.a.add("Lunch", T1, T2)
        run(self.cals)
        s = run(self.cals, rules(a_b={"filters": {"keywords": ["lunch"]}}))
        self.assertEqual((s["deleted"], self.b.copies()), (1, []))

    def test_clean_rule_holds_values_to_what_is_known(self):
        r = calsync.clean_rule({"tag": "x\x00" * 50, "colour": "chartreuse", "copy": {"title_text": "", "bogus": True},
                                "filters": {"days": ["1", "9", "x"], "from": "25:00", "until": "18:30", "colours": "red, mauve", "keywords": "a, ,b"}})
        self.assertEqual((len(r["tag"]), r["colour"], r["copy"]["title_text"]), (40, "", "Busy"))
        self.assertNotIn("bogus", r["copy"])
        self.assertEqual((r["filters"]["days"], r["filters"]["from"], r["filters"]["until"]), ([1], "", "18:30"))
        self.assertEqual((r["filters"]["colours"], r["filters"]["keywords"]), (["red"], ["a", "b"]))


class WindowTests(unittest.TestCase):
    def test_days_back_and_ahead(self):
        a, b = FakeCal(), FakeCal()
        a.add("Last week", "2026-10-26T15:00:00Z", "2026-10-26T16:00:00Z")
        a.add("Far off", "2027-01-20T15:00:00Z", "2027-01-20T16:00:00Z")
        run({"a": a, "b": b})
        self.assertEqual([e["title"] for e in b.copies()], ["Far off"])      # by default: nothing past, 120 days ahead
        run({"a": a, "b": b}, {"*": {"window": {"past_days": 30, "future_days": 30}}})
        self.assertEqual(sorted(e["title"] for e in b.copies()), ["Far off", "Last week"])     # a copy outside the window is left as it is

    def test_window_is_held_to_bounds(self):
        lo, hi = calsync.sync_window({"window": {"past_days": -5, "future_days": 99999}}, NOW)
        self.assertEqual((lo, hi[:10]), ("2026-11-01T12:00:00Z", "2028-10-31"))


class WhyTests(unittest.TestCase):
    def test_every_source_event_says_why(self):
        a, b, c = FakeCal(), FakeCal(), FakeCal()
        a.add("Copied", T1, T2)
        a.add("Declined", T1, T2, declined=True)
        a.add("Lunch", T1, T2)
        a.add("Invite", T1, T2, ical="shared@x")
        b.add("Invite", T1, T2, ical="shared@x")
        decisions = []
        run({"a": a, "b": b, "c": c}, {"*": {"rules": {"a>b": {"filters": {"keywords": ["lunch"]}}, "a>c": {"enabled": False}}},
                                       "c": {"read_only": False}}, decisions=decisions)
        why = {(d["title"], d["dst"]): d["reason"] for d in decisions if d["src"] == "a"}
        self.assertTrue(why[("Copied", "b")].startswith('copied as "Copied"'))
        self.assertEqual(why[("Declined", "")], "you declined it")
        self.assertEqual(why[("Lunch", "b")], 'its title has "lunch"')
        self.assertEqual(why[("Invite", "b")], "it's already on b through an invite")
        self.assertEqual(why[("Copied", "c")], "copying from a to c is off")

    def test_a_copy_to_remove_says_so(self):
        a, b = FakeCal(), FakeCal()
        uid = a.add("Gone soon", T1, T2)
        run({"a": a, "b": b})
        del a.events[uid]
        decisions = []
        run({"a": a, "b": b}, decisions=decisions, dry_run=True)
        self.assertEqual([d["action"] for d in decisions], ["remove"])


class KeepCopiesTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeCal(), FakeCal()
        self.cals = {"a": self.a, "b": self.b}
        self.a.add("Class", T1, T2)
        run(self.cals)

    def test_turning_a_direction_off_removes_or_keeps_its_copies(self):
        run(self.cals, rules(a_b={"enabled": False, "keep_copies": True}))
        self.assertEqual(len(self.b.copies()), 1)
        self.a.events[next(iter(self.a.natives()))["uid"]]["title"] = "Renamed"
        run(self.cals, rules(a_b={"enabled": False, "keep_copies": True}))
        self.assertEqual(self.b.copies()[0]["title"], "Class")            # kept, and no longer updated
        run(self.cals, rules(a_b={"enabled": False}))
        self.assertEqual(self.b.copies(), [])

    def test_a_removed_calendar_can_leave_its_copies(self):
        s = calsync.sync([{"name": "b"}], {"b": self.b}, now=NOW, log=lambda *_: None, cfg={"kept_sources": ["a"]})
        self.assertEqual((s["deleted"], len(self.b.copies())), (0, 1))
        calsync.sync([{"name": "b"}, {"name": "x"}], {"b": self.b, "x": FakeCal()}, now=NOW, log=lambda *_: None, cfg={})
        self.assertEqual(self.b.copies(), [])


class ResyncTests(unittest.TestCase):
    def test_one_pair_only_and_refresh_rewrites(self):
        a, b, c = FakeCal(), FakeCal(), FakeCal()
        a.add("A", T1, T2)
        b.add("B", T1, T2)
        run({"a": a, "b": b, "c": c})
        before = {n: x.writes for n, x in (("a", a), ("b", b), ("c", c))}
        s = run({"a": a, "b": b, "c": c}, pair="a>b", refresh=True)
        self.assertEqual((s["updated"], list(s["pairs"])), (1, ["a>b"]))
        self.assertEqual((a.writes, c.writes), (before["a"], before["c"]))
        self.assertEqual(b.writes, before["b"] + 1)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.old = calsync.HOME
        calsync.set_home(self.home)

    def tearDown(self):
        calsync.set_home(self.old)

    def test_runs_are_logged_newest_first_and_per_pair(self):
        a, b = FakeCal(), FakeCal()
        a.add("Class", T1, T2)
        calsync.save_json(calsync.CONFIG, {"calendars": [{"name": "a", "account": "x"}, {"name": "b", "account": "y"}], "enabled": True})
        with mock.patch.object(calsync, "make_adapter", lambda c: {"a": a, "b": b}[c["name"]]), mock.patch.object(calsync, "now_utc", return_value=NOW):
            calsync.run_once(log=lambda *_: None, trigger="manual")
            calsync.run_once(log=lambda *_: None)
        runs = calsync.activity()
        self.assertEqual([r["trigger"] for r in runs], ["timer", "manual"])
        self.assertEqual(runs[1]["pairs"]["a>b"]["created"], 1)
        self.assertEqual(runs[1]["lines"], ["+ b: Class 2026-11-02T15:00:00Z"])
        self.assertEqual(len(calsync.activity(pair="a>b")), 1)
        status = calsync.pair_status()
        self.assertEqual((status["a>b"][0], status["b>a"][0]), ("synced", "synced"))
        self.assertEqual(oct(os.stat(calsync.ACTIVITY).st_mode & 0o777) if os.name != "nt" else "0o600", "0o600")

    def test_log_is_trimmed(self):
        for i in range(calsync.ACTIVITY_KEEP + 5):
            calsync.record_activity({"at": str(i)})
        runs = calsync.activity(limit=1000)
        self.assertEqual((len(runs), runs[0]["at"]), (calsync.ACTIVITY_KEEP, str(calsync.ACTIVITY_KEEP + 4)))

    def test_set_home_moves_every_path(self):
        self.assertTrue(calsync.CONFIG.startswith(self.home) and calsync.SECRETS.startswith(self.home) and calsync.ACTIVITY.startswith(self.home))


class ProviderFieldTests(unittest.TestCase):
    def test_google_reads_what_filters_need(self):
        ev = calsync.GoogleCalendar._norm({
            "id": "x", "summary": "Sync", "status": "confirmed", "colorId": "11", "eventType": "outOfOffice",
            "start": {"dateTime": "2026-11-02T10:00:00-05:00"}, "end": {"dateTime": "2026-11-02T11:00:00-05:00"},
            "hangoutLink": "https://meet.google.com/abc", "organizer": {"email": "boss@x.com"},
            "attendees": [{"self": True, "responseStatus": "tentative", "email": "me@x.com"}, {"email": "boss@x.com"}]})
        self.assertEqual((ev["rsvp"], ev["kind"], ev["colours"], ev["conference"], ev["attendees"]),
                         ("maybe", "ooo", ["red"], "https://meet.google.com/abc", ["boss@x.com"]))
        bday = calsync.GoogleCalendar._norm({"id": "b", "eventType": "birthday", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}})
        self.assertEqual(bday["kind"], "birthday")
        mine = calsync.GoogleCalendar._norm({"id": "m", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}})
        self.assertEqual((mine["rsvp"], mine["me_organizer"]), ("", True))

    def test_outlook_reads_answers_and_categories(self):
        ev = calsync.OutlookCalendar._norm({
            "id": "o", "subject": "Review", "isOrganizer": False, "showAs": "tentative", "categories": ["Blue"],
            "start": {"dateTime": "2026-11-02T15:30:00.0000000"}, "end": {"dateTime": "2026-11-02T16:00:00.0000000"},
            "responseStatus": {"response": "notResponded"}, "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/1"},
            "body": {"contentType": "text", "content": "Agenda"}})
        self.assertEqual((ev["rsvp"], ev["colours"], ev["conference"], ev["description"]), ("none", ["blue"], "https://teams.microsoft.com/l/meetup-join/1", "Agenda"))

    def test_ics_reads_answers_categories_and_links(self):
        text = "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:u1", "DTSTART:20261102T150000Z", "DTEND:20261102T160000Z", "SUMMARY:Call",
                            "ORGANIZER:mailto:boss@x.com", "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@x.com", "CATEGORIES:Work,Red",
                            "DESCRIPTION:Join https://zoom.us/j/123?pwd=a now", "END:VEVENT", "END:VCALENDAR"])
        ev = calsync.ics_events(text, "2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z", me={"me@x.com"})[0]
        self.assertEqual((ev["rsvp"], ev["colours"], ev["conference"], ev["organizer"]), ("none", ["work", "red"], "https://zoom.us/j/123?pwd=a", "boss@x.com"))


class EditorTests(unittest.TestCase):
    def test_repeat_rules_and_clean_event(self):
        self.assertEqual(calsync.repeat_rule("weekly", "2026-11-03T10:00"), "FREQ=WEEKLY;BYDAY=TU")
        self.assertTrue(calsync.repeat_rule("daily", "2026-11-03T10:00", until="2026-11-30").startswith("FREQ=DAILY;UNTIL=20261201T"))
        ev = calsync.clean_event({"title": " Lunch ", "start": "2026-11-03T12:00", "end": "2026-11-03T11:00", "guests": "a@b.com, nope, A@B.com"})
        self.assertEqual((ev["title"], ev["end"] - ev["start"], ev["guests"]), ("Lunch", dt.timedelta(hours=1), ["a@b.com"]))
        day = calsync.clean_event({"title": "Trip", "start": "2026-11-05", "end": "2026-11-07", "all_day": True, "end_inclusive": True})
        self.assertEqual((day["start"], day["end"]), (dt.date(2026, 11, 5), dt.date(2026, 11, 8)))
        with self.assertRaises(ValueError):
            calsync.clean_event({"title": "x", "start": "2026-11-03T12:00", "repeat": "FREQ=SECONDLY"})
        self.assertEqual(calsync.clean_event({"title": "x"}, partial=True), {"title": "x"})

    def test_search_words_and_filters(self):
        q = calsync.parse_search("Dentist cal:Personal from:sam before:2026-12-01 after:nope")
        self.assertEqual((q["words"], q["cal"], q["from"], q["before"]), (["dentist", "after:nope"], "personal", "sam", dt.date(2026, 12, 1)))

    def test_search_finds_real_events_only(self):
        a, b = FakeCal(), FakeCal()
        a.add("Dentist checkup", "2026-11-20T15:00:00Z", "2026-11-20T16:00:00Z", organizer="sam@x.com")
        b.add("Dentist checkup", "2026-11-20T15:00:00Z", "2026-11-20T16:00:00Z", copy_of="a|1")
        b.add("Gym", "2026-11-21T15:00:00Z", "2026-11-21T16:00:00Z")
        cfg = {"calendars": [{"name": "a", "label": "Work"}, {"name": "b", "label": "Home"}]}
        with mock.patch.object(calsync, "make_adapter", lambda c: {"a": a, "b": b}[c["name"]]):
            rows, _ = calsync.search_events("dentist", cfg, now=NOW)
            self.assertEqual([(r["title"], r["calendar"]) for r in rows], [("Dentist checkup", "a")])
            self.assertEqual(calsync.search_events("gym cal:work", cfg, now=NOW)[0], [])
            self.assertEqual(len(calsync.search_events("from:sam", cfg, now=NOW)[0]), 1)

    def test_editing_a_copy_opens_the_original(self):
        cfg = {"calendars": [{"name": "work"}, {"name": "home"}]}
        self.assertEqual(calsync.original_of("work|abc|2026", cfg), ({"name": "work"}, "abc|2026"))
        self.assertIsNone(calsync.original_of("gone|abc", cfg))


class CalDavEditTests(unittest.TestCase):
    """The Calendar tab's edits on a CalDAV calendar, against an in-memory server."""
    SERIES = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:weekly@x", "DTSTART:20261102T150000Z", "DTEND:20261102T160000Z",
                          "RRULE:FREQ=WEEKLY;COUNT=10", "SUMMARY:Class", "BEGIN:VALARM", "TRIGGER:-PT5M", "DESCRIPTION:keep me", "END:VALARM",
                          "END:VEVENT", "END:VCALENDAR"]) + "\r\n"

    def setUp(self):
        self.files = {}
        files = self.files

        class Client:
            def request(self, method, url, body=None, headers=None, depth=None):
                if method == "GET":
                    return (200, files[url].encode(), url) if url in files else (404, b"", url)
                if method == "PUT":
                    files[url] = body
                    return 201, b"", url
                if method == "DELETE":
                    files.pop(url, None)
                    return 204, b"", url
        self.cal = calsync.CalDavCalendar.__new__(calsync.CalDavCalendar)
        self.cal.client, self.cal.url, self.cal.me, self.cal.tag_checked = Client(), "https://dav.x/cal/", set(), True
        self.href = "https://dav.x/cal/weekly.ics"
        files[self.href] = self.SERIES

    def events(self):
        out = []
        for text in self.files.values():
            out += calsync.ics_events(text, "2026-11-01T00:00:00Z", "2027-02-01T00:00:00Z")
        return sorted(out, key=lambda e: e["start"])

    def test_change_only_this_one(self):
        orig = "2026-11-09T15:00:00+00:00"
        self.cal.update_event(f"weekly@x|{orig}", {"title": "Moved class", "start": calsync._aware("2026-11-09T17:00:00+00:00"),
                                                    "end": calsync._aware("2026-11-09T18:00:00+00:00")}, "this", href=self.href)
        evs = self.events()
        self.assertEqual(len(evs), 10)
        self.assertEqual([(e["title"], e["start"]) for e in evs if e["title"] != "Class"], [("Moved class", "2026-11-09T17:00:00Z")])
        self.assertIn("DESCRIPTION:keep me", self.files[self.href])          # the reminder inside is untouched

    def test_change_every_one_moves_the_series(self):
        self.cal.update_event("weekly@x|2026-11-09T15:00:00+00:00", {"start": calsync._aware("2026-11-09T16:00:00+00:00"),
                                                                     "end": calsync._aware("2026-11-09T17:00:00+00:00")}, "all", href=self.href)
        self.assertEqual({e["start"][11:] for e in self.events()}, {"16:00:00Z"})

    def test_this_and_following_splits_the_series(self):
        self.cal.update_event("weekly@x|2026-11-16T15:00:00+00:00", {"title": "New time"}, "following", href=self.href)
        evs = self.events()
        self.assertEqual([e["title"] for e in evs][:3], ["Class", "Class", "New time"])
        self.assertEqual(len(self.files), 2)

    def test_delete_one_and_the_rest(self):
        self.cal.delete_event("weekly@x|2026-11-09T15:00:00+00:00", "this", href=self.href)
        self.assertNotIn("2026-11-09T15:00:00Z", [e["start"] for e in self.events()])
        self.cal.delete_event("weekly@x|2026-11-23T15:00:00+00:00", "following", href=self.href)
        self.assertEqual([e["start"][:10] for e in self.events()], ["2026-11-02", "2026-11-16"])
        self.cal.delete_event("weekly@x|2026-11-02T15:00:00+00:00", "all", href=self.href)
        self.assertEqual(self.files, {})

    def test_an_event_elsewhere_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.cal.update_event("x|y", {"title": "no"}, href="https://evil.example/cal/x.ics")

    def test_create(self):
        href = self.cal.create_event(calsync.clean_event({"title": "Lunch", "start": "2026-11-04T12:00:00-05:00", "end": "2026-11-04T13:00:00-05:00",
                                                          "location": "Cafe", "repeat": "FREQ=DAILY;COUNT=2", "busy": False}))
        evs = calsync.ics_events(self.files[href], "2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z")
        self.assertEqual([(e["title"], e["start"], e["location"], e["busy"]) for e in evs],
                         [("Lunch", "2026-11-04T17:00:00Z", "Cafe", False), ("Lunch", "2026-11-05T17:00:00Z", "Cafe", False)])


class GoogleEditTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        master = {"id": "m", "start": {"dateTime": "2026-11-02T10:00:00-05:00"}, "end": {"dateTime": "2026-11-02T11:00:00-05:00"},
                  "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=10"], "summary": "Class"}
        inst = {"id": "m_20261109T150000Z", "recurringEventId": "m", "start": {"dateTime": "2026-11-09T10:00:00-05:00"},
                "end": {"dateTime": "2026-11-09T11:00:00-05:00"}, "originalStartTime": {"dateTime": "2026-11-09T10:00:00-05:00"}}

        def fake(method, url, token=None, body=None, **kw):
            self.calls.append((method, url.split("/events")[-1], body))
            if method == "GET":
                return master if url.split("?")[0].endswith("/m") else inst
            return {"id": "new"}
        self.p = mock.patch.object(calsync, "http", fake)
        self.p.start()
        self.g = calsync.GoogleCalendar("acct", "cal")
        self.g._token = "t"

    def tearDown(self):
        self.p.stop()

    def test_scopes(self):
        self.g.update_event("m_20261109T150000Z", {"title": "Only this"}, "this", notify=False)
        self.assertEqual(self.calls[-1][:2], ("PATCH", "/m_20261109T150000Z?conferenceDataVersion=1&sendUpdates=none"))
        self.g.update_event("m_20261109T150000Z", {"start": calsync._aware("2026-11-09T11:00:00-05:00"), "end": calsync._aware("2026-11-09T12:00:00-05:00")}, "all")
        method, path, body = self.calls[-1]
        self.assertTrue(path.startswith("/m?"))
        self.assertEqual(calsync._aware(body["start"]["dateTime"]), calsync._aware("2026-11-02T11:00:00-05:00"))     # every one moves an hour
        self.calls.clear()
        self.g.update_event("m_20261109T150000Z", {"title": "From here"}, "following")
        patch = next(c for c in self.calls if c[0] == "PATCH")
        self.assertEqual(patch[2]["recurrence"], ["RRULE:FREQ=WEEKLY;UNTIL=20261109T145959Z"])
        post = next(c for c in self.calls if c[0] == "POST")
        self.assertEqual((post[2]["summary"], post[2]["recurrence"]), ("From here", ["RRULE:FREQ=WEEKLY"]))

    def test_answer(self):
        self.calls.clear()
        with mock.patch.object(self.g, "_one", return_value={"attendees": [{"self": True, "email": "me@x"}, {"email": "boss@x"}]}):
            self.g.respond("e1", "maybe")
        self.assertEqual(self.calls[-1][2]["attendees"][0]["responseStatus"], "tentative")


if __name__ == "__main__":
    unittest.main()
