"""Engine tests against in-memory calendars: python3 -m unittest test_calsync"""
import contextlib, datetime as dt, io, itertools, json, os, stat, tempfile, time, unittest
from unittest import mock

os.environ.setdefault("CALSYNC_HOME", tempfile.mkdtemp())    # never your real one
os.environ["CALSYNC_TZ"] = "America/New_York"   # the tests' expected local times
import calsync

NOW = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)
ids = itertools.count(1)


class FakeCal:
    def __init__(self):
        self.events = {}
        self.fail = False
        self.writes = 0

    def add(self, title, start, end, **kw):
        uid = f"e{next(ids)}"
        ev = {"uid": uid, "ical": kw.pop("ical", uid + "@fake"), "title": title, "start": start, "end": end,
              "all_day": len(start) == 10, "busy": True, "location": "", "private": False,
              "declined": False, "copy_of": None}
        ev.update(kw)
        self.events[uid] = ev
        return uid

    def list(self, tmin, tmax):
        if self.fail:
            raise RuntimeError("boom")
        return [dict(e) for e in self.events.values() if e["end"] >= tmin[:len(e["end"])] and e["start"] <= tmax[:len(e["start"])]]

    def create(self, fields, copy_of, label):
        self.writes += 1
        uid = f"c{next(ids)}"
        self.events[uid] = dict(fields, uid=uid, ical=uid + "@copy", private=False, declined=False, copy_of=copy_of)
        return uid

    def update(self, uid, fields, copy_of, label):
        self.writes += 1
        self.events[uid].update(fields, copy_of=copy_of)

    def delete(self, uid):
        self.writes += 1
        del self.events[uid]

    def copies(self):
        return [e for e in self.events.values() if e["copy_of"]]

    def natives(self):
        return [e for e in self.events.values() if not e["copy_of"]]


def run(cals, cfg=None, **kw):
    cfgs = [dict({"name": n}, **(cfg or {}).get(n, {})) for n in cals]
    return calsync.sync(cfgs, cals, now=NOW, log=lambda *_: None, **kw)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = FakeCal(), FakeCal(), FakeCal()
        self.cals = {"a": self.a, "b": self.b, "c": self.c}

    def test_copies_everywhere_and_idempotent(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.b.add("Trip", "2026-11-05", "2026-11-07")
        s = run(self.cals)
        self.assertEqual(s["created"], 4)  # a->b, a->c, b->a, b->c
        self.assertEqual({e["title"] for e in self.c.copies()}, {"Class", "Trip"})
        self.assertTrue(any(e["all_day"] for e in self.c.copies()))
        writes = sum(x.writes for x in self.cals.values())
        s2 = run(self.cals)
        self.assertEqual((s2["created"], s2["updated"], s2["deleted"]), (0, 0, 0))
        self.assertEqual(sum(x.writes for x in self.cals.values()), writes)

    def test_never_copies_a_copy(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        for _ in range(3):
            run(self.cals)
        self.assertEqual(len(self.b.events), 1)
        self.assertEqual(len(self.c.events), 1)
        self.assertEqual(len(self.a.events), 1)

    def test_move_and_delete_follow_the_source(self):
        uid = self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run(self.cals)
        self.a.events[uid]["start"] = "2026-11-02T17:00:00Z"
        self.a.events[uid]["end"] = "2026-11-02T18:00:00Z"
        self.assertEqual(run(self.cals)["updated"], 2)
        self.assertEqual(self.b.copies()[0]["start"], "2026-11-02T17:00:00Z")
        del self.a.events[uid]
        self.assertEqual(run(self.cals)["deleted"], 2)
        self.assertEqual(self.b.events, {})

    def test_edit_to_a_copy_is_overwritten(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run(self.cals)
        self.b.copies()[0]["title"] = "renamed by hand"
        self.assertEqual(run(self.cals)["updated"], 1)
        self.assertEqual(self.b.copies()[0]["title"], "Class")

    def test_declined_private_and_busy_only(self):
        self.a.add("Declined", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", declined=True)
        self.a.add("Therapy", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z", private=True, location="X")
        self.a.add("Client call", "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", location="Zoom")
        run(self.cals, cfg={"c": {"busy_only": True}})
        self.assertEqual(sorted(e["title"] for e in self.b.copies()), ["Busy", "Client call"])
        self.assertEqual([e["location"] for e in self.b.copies() if e["title"] == "Busy"], [""])
        self.assertEqual(sorted(e["title"] for e in self.c.copies()), ["Busy", "Busy"])

    def test_canceled_meetings_not_copied(self):
        self.a.add("Canceled: Leadership Meeting", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.a.add("Class", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")
        run(self.cals)
        self.assertEqual([e["title"] for e in self.b.copies()], ["Class"])

    def test_skip_sources(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.b.add("Client", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")
        run(self.cals, cfg={"c": {"skip_sources": ["a"]}})
        self.assertEqual([e["title"] for e in self.c.copies()], ["Client"])

    def test_free_events_stay_free(self):
        self.a.add("Reminder", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", busy=False)
        run(self.cals)
        self.assertFalse(self.b.copies()[0]["busy"])

    def test_read_only_calendar_is_a_source_only(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.b.add("Mine", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")
        run(self.cals, cfg={"a": {"read_only": True}})
        self.assertEqual(self.a.copies(), [])
        self.assertEqual({e["title"] for e in self.c.copies()}, {"Class", "Mine"})

    def test_failed_source_keeps_its_copies(self):
        for i in range(5):
            self.a.add(f"Class {i}", f"2026-11-0{i + 2}T15:00:00Z", f"2026-11-0{i + 2}T16:00:00Z")
        run(self.cals)
        self.a.fail = True
        s = run(self.cals)
        self.assertEqual(s["failed"], ["a"])
        self.assertEqual(s["deleted"], 0)
        self.assertEqual(len(self.b.copies()), 5)

    def test_mass_delete_guard(self):
        for i in range(30):
            self.a.add(f"E{i}", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run(self.cals)
        self.a.events = {}  # e.g. an API returning an empty list by mistake
        s = run(self.cals)
        self.assertEqual(s["deleted"], 0)
        self.assertEqual(s["skipped_deletes"], 60)
        self.assertEqual(len(self.b.copies()), 30)
        self.assertEqual(run(self.cals, force=True)["deleted"], 60)

    def test_duplicate_copies_are_cleaned(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run(self.cals)
        c = self.b.copies()[0]
        self.b.events["dup"] = dict(c, uid="dup")
        self.assertEqual(run(self.cals)["deleted"], 1)
        self.assertEqual(len(self.b.copies()), 1)

    def test_same_meeting_on_two_accounts_is_not_doubled(self):
        self.a.add("Standup", "2026-11-02T15:00:00Z", "2026-11-02T15:30:00Z", ical="meet-1")
        self.b.add("Standup", "2026-11-02T15:00:00Z", "2026-11-02T15:30:00Z", ical="meet-1")
        run(self.cals)
        self.assertEqual(self.a.copies(), [])
        self.assertEqual(self.b.copies(), [])
        self.assertEqual(len(self.c.copies()), 2)  # c has neither: one copy from each (same time)

    def test_never_touches_events_it_did_not_create(self):
        self.b.add("My own", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        before = dict(self.b.natives()[0])
        self.a.events = {}
        run(self.cals, force=True)
        self.assertEqual(self.b.natives()[0], before)

    def test_removed_calendar_copies_go_away(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run(self.cals)
        del self.cals["a"]
        self.assertEqual(run(self.cals)["deleted"], 2)

    def test_dry_run_writes_nothing(self):
        self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        s = run(self.cals, dry_run=True)
        self.assertEqual(s["created"], 2)
        self.assertEqual(sum(x.writes for x in self.cals.values()), 0)


class NormaliseTests(unittest.TestCase):
    def test_google(self):
        ev = calsync.GoogleCalendar._norm({
            "id": "x", "iCalUID": "x@google.com", "summary": "Call", "status": "confirmed",
            "start": {"dateTime": "2026-11-02T10:30:00-05:00"}, "end": {"dateTime": "2026-11-02T11:00:00-05:00"},
            "transparency": "transparent", "attendees": [{"self": True, "responseStatus": "declined"}],
            "extendedProperties": {"private": {"calsync": "a|1"}}})
        self.assertEqual((ev["start"], ev["end"], ev["busy"], ev["declined"], ev["copy_of"]),
                         ("2026-11-02T15:30:00Z", "2026-11-02T16:00:00Z", False, True, "a|1"))
        self.assertIsNone(calsync.GoogleCalendar._norm({"id": "y", "status": "cancelled"}))
        self.assertIsNone(calsync.GoogleCalendar._norm({"id": "z", "eventType": "workingLocation", "start": {}, "end": {}}))

    def test_outlook(self):
        ev = calsync.OutlookCalendar._norm({
            "id": "o", "iCalUId": "abc", "subject": "Lecture", "isAllDay": False, "showAs": "busy",
            "start": {"dateTime": "2026-11-02T15:30:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-02T16:00:00.0000000", "timeZone": "UTC"},
            "location": {"displayName": "Room 2"}, "sensitivity": "normal",
            "responseStatus": {"response": "accepted"},
            "singleValueExtendedProperties": [{"id": calsync.MS_PROP, "value": "b|2"}]})
        self.assertEqual((ev["start"], ev["end"], ev["busy"], ev["location"], ev["copy_of"]),
                         ("2026-11-02T15:30:00Z", "2026-11-02T16:00:00Z", True, "Room 2", "b|2"))
        day = calsync.OutlookCalendar._norm({"id": "d", "isAllDay": True, "start": {"dateTime": "2026-11-05T00:00:00.0000000"},
                                             "end": {"dateTime": "2026-11-06T00:00:00.0000000"}})
        self.assertEqual((day["start"], day["end"], day["all_day"]), ("2026-11-05", "2026-11-06", True))

    def test_bodies_round_trip(self):
        f = {"title": "T", "start": "2026-11-02T15:30:00Z", "end": "2026-11-02T16:00:00Z", "all_day": False, "busy": True, "location": "L"}
        g = calsync.GoogleCalendar("acct", "cal")._body(f, "a|1", "A")
        back = calsync.GoogleCalendar._norm(dict(g, id="n", status="confirmed"))
        self.assertEqual(calsync.comparable(back), f)
        o = calsync.OutlookCalendar("acct", "cal")._body(f, "a|1", "A")
        back = calsync.OutlookCalendar._norm(dict(o, id="n", start={"dateTime": o["start"]["dateTime"] + ".0000000"},
                                                  end={"dateTime": o["end"]["dateTime"] + ".0000000"}, location=o["location"]))
        self.assertEqual(calsync.comparable(back), f)
        fd = dict(f, all_day=True, start="2026-11-05", end="2026-11-06")
        o = calsync.OutlookCalendar("acct", "cal")._body(fd, "a|1", "A")
        back = calsync.OutlookCalendar._norm(dict(o, id="n"))
        self.assertEqual(calsync.comparable(back), fd)


ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VTIMEZONE", "TZID:Eastern Standard Time", "END:VTIMEZONE",
    # weekly class Mon+Wed 09:30-10:45 Eastern, across the 11-01 DST change, one skipped day, one moved, one cancelled
    "BEGIN:VEVENT", "UID:class-1", "SUMMARY:INFO-I 300 Lecture\\, Section 2",
    "DTSTART;TZID=Eastern Standard Time:20261026T093000", "DTEND;TZID=Eastern Standard Time:20261026T104500",
    "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261209T235900", "EXDATE;TZID=Eastern Standard Time:20261111T093000",
    "LOCATION:Room 12", "X-MICROSOFT-CDO-BUSYSTATUS:BUSY", "END:VEVENT",
    "BEGIN:VEVENT", "UID:class-1", "RECURRENCE-ID;TZID=Eastern Standard Time:20261104T093000",
    "SUMMARY:INFO-I 300 Lecture (moved)", "DTSTART;TZID=Eastern Standard Time:20261104T130000",
    "DTEND;TZID=Eastern Standard Time:20261104T141500", "END:VEVENT",
    "BEGIN:VEVENT", "UID:class-1", "RECURRENCE-ID;TZID=Eastern Standard Time:20261116T093000",
    "STATUS:CANCELLED", "DTSTART;TZID=Eastern Standard Time:20261116T093000", "END:VEVENT",
    # all-day, folded long title, free
    "BEGIN:VEVENT", "UID:break-1", "SUMMARY:Thanksgiving",
    "  break", "DTSTART;VALUE=DATE:20261125", "DTEND;VALUE=DATE:20261130", "TRANSP:TRANSPARENT", "END:VEVENT",
    # UTC with DURATION, private
    "BEGIN:VEVENT", "UID:advising", "SUMMARY:Advising", "CLASS:PRIVATE", "DTSTART:20261103T190000Z", "DURATION:PT30M", "END:VEVENT",
    # long gone: outside window
    "BEGIN:VEVENT", "UID:old", "SUMMARY:Old", "DTSTART:20260101T190000Z", "DTEND:20260101T200000Z", "END:VEVENT",
    "END:VCALENDAR", ""])


class DueItemsTests(unittest.TestCase):
    LO, HI = "2026-11-04T15:00:00Z", "2027-03-04T15:00:00Z"

    def item(self, start, end, uid="feed-homework-1", all_day=True):
        return {"uid": uid, "ical": uid, "title": "HW", "start": start, "end": end, "all_day": all_day,
                "busy": True, "location": "", "private": False, "declined": False, "copy_of": None}

    def shape(self, *evs, **kw):
        return calsync.due_items(list(evs), kw.pop("lo", self.LO), self.HI, kw.get("only"), kw.get("free", False))

    def test_only_keeps_matching_uids(self):
        keep, drop = self.item("2026-11-09", "2026-11-10"), self.item("2026-11-09", "2026-11-10", "feed-meeting-2")
        self.assertEqual(self.shape(keep, drop, only="homework"), [keep])

    def test_free_never_marks_busy(self):
        self.assertFalse(self.shape(self.item("2026-11-09", "2026-11-10"), free=True)[0]["busy"])

    def test_all_day_item_goes_when_its_day_is_over_here(self):
        ev = self.item("2026-11-09", "2026-11-10")
        self.assertEqual(len(self.shape(ev, lo="2026-11-10T04:59:00Z")), 1)   # still 11:59 PM on the 9th
        self.assertEqual(self.shape(ev, lo="2026-11-10T05:00:00Z"), [])                        # midnight in Indianapolis

    def test_item_due_yesterday_is_dropped(self):
        self.assertEqual(self.shape(self.item("2026-11-03", "2026-11-04")), [])

    def test_all_day_item_at_the_far_edge_waits(self):
        self.assertEqual(self.shape(self.item("2027-03-03", "2027-03-04")), [])
        self.assertEqual(len(self.shape(self.item("2027-03-01", "2027-03-02"))), 1)

    def test_shown_to_people_the_far_edge_stays(self):
        # an agenda or the Calendar tab must not lose what's due in its last two days (HW due 12-01, asked on 11-26 for 7 days)
        edge = self.item("2027-03-03", "2027-03-04")
        self.assertEqual(calsync.due_items([edge], self.LO, self.HI, free=True, for_sync=False)[0]["uid"], edge["uid"])
        self.assertEqual(calsync.due_items([edge], self.LO, self.HI, free=True), [])      # a sync still waits

    def test_list_for_people_asks_a_due_feed_without_the_trim(self):
        feed = calsync.IcsCalendar("https://feed.test/x.ics", free=True)
        with mock.patch.object(calsync.IcsCalendar, "list", return_value=[]) as listed:
            calsync.list_for_people(feed, self.LO, self.HI)
        listed.assert_called_once_with(self.LO, self.HI, for_sync=False)

        class Other:
            def list(self, tmin, tmax):
                return ["ok"]
        self.assertEqual(calsync.list_for_people(Other(), self.LO, self.HI), ["ok"])

    def test_due_time_gets_a_length_ending_at_the_deadline(self):
        ev = self.shape(self.item("2026-11-10T22:30:00Z", "2026-11-10T22:30:00Z", all_day=False))[0]
        self.assertEqual((ev["start"], ev["end"]), ("2026-11-10T22:00:00Z", "2026-11-10T22:30:00Z"))

    def test_plain_feed_is_untouched(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return ICS.encode()
        with mock.patch.object(calsync.urllib.request, "urlopen", lambda *a, **k: Reply()):
            self.assertEqual(calsync.IcsCalendar("https://feed.test/x.ics").list("2026-10-31T12:00:00Z", "2026-12-31T00:00:00Z"),
                             calsync.ics_events(ICS, "2026-10-31T12:00:00Z", "2026-12-31T00:00:00Z"))


class IcsTests(unittest.TestCase):
    def setUp(self):
        self.evs = calsync.ics_events(ICS, "2026-10-31T12:00:00Z", "2026-12-31T00:00:00Z")
        self.by = lambda t: sorted((e for e in self.evs if e["title"].startswith(t)), key=lambda e: e["start"])

    def test_weekly_class_dst_exdate_override_cancel(self):
        lec = self.by("INFO-I 300 Lecture")
        starts = [e["start"] for e in lec]
        self.assertEqual(starts[0], "2026-11-02T14:30:00Z")          # 09:30 EST after the change
        self.assertNotIn("2026-11-11T14:30:00Z", starts)              # EXDATE
        self.assertNotIn("2026-11-16T14:30:00Z", starts)              # cancelled occurrence
        self.assertNotIn("2026-11-04T14:30:00Z", starts)              # moved away from its slot...
        self.assertIn("2026-11-04T18:00:00Z", starts)                 # ...to 13:00 EST
        self.assertEqual([e["title"] for e in lec if e["start"] == "2026-11-04T18:00:00Z"], ["INFO-I 300 Lecture (moved)"])
        self.assertEqual(starts[-1], "2026-12-09T14:30:00Z")          # UNTIL respected
        self.assertEqual(len(starts), 10)                             # 12 Mon/Wed slots Nov 2..Dec 9, minus EXDATE and cancelled
        self.assertEqual(lec[0]["title"], "INFO-I 300 Lecture, Section 2")
        self.assertEqual((lec[0]["end"], lec[0]["location"], lec[0]["busy"]), ("2026-11-02T15:45:00Z", "Room 12", True))
        self.assertEqual(len({e["uid"] for e in lec}), len(lec))      # stable, unique per occurrence

    def test_all_day_folded_free(self):
        (b,) = self.by("Thanksgiving")
        self.assertEqual((b["title"], b["start"], b["end"], b["all_day"], b["busy"]), ("Thanksgiving break", "2026-11-25", "2026-11-30", True, False))

    def test_duration_private_and_window(self):
        (a,) = self.by("Advising")
        self.assertEqual((a["start"], a["end"], a["private"]), ("2026-11-03T19:00:00Z", "2026-11-03T19:30:00Z", True))
        self.assertEqual(self.by("Old"), [])

    def test_ics_source_is_never_written(self):
        class FakeIcs:
            read_only = True
            def list(self, tmin, tmax):
                return calsync.ics_events(ICS, tmin, tmax)
        a = FakeCal()
        a.add("Gym", "2026-11-02T22:00:00Z", "2026-11-02T23:00:00Z")
        cals = {"school": FakeIcs(), "a": a}
        s = calsync.sync([{"name": "school"}, {"name": "a"}], cals, now=NOW, log=lambda *_: None)
        self.assertEqual(s["errors"], 0)
        self.assertTrue(any(e["title"] == "INFO-I 300 Lecture, Section 2" for e in a.copies()))
        self.assertTrue(any(e["title"] == "Busy" for e in a.copies()))   # private advising
        self.assertEqual(calsync.sync([{"name": "school"}, {"name": "a"}], cals, now=NOW, log=lambda *_: None)["created"], 0)

    def test_ics_feed_of_a_calendar_holding_copies_does_not_copy_them_again(self):
        # Google's secret address publishes a calendar's events without calsync's tag. The copies calsync once wrote there
        # still carry its note, so they are known as copies and only the calendar's own events are copied on.
        feed = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:own-1@google.com\r\nSUMMARY:Coffee with Sam\r\n"
                "DTSTART:20261103T150000Z\r\nDTEND:20261103T160000Z\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:old-copy-1@google.com\r\nSUMMARY:Standup\r\n"
                "DESCRIPTION:Synced from work by calsync. Edit the original there\\; changes here are overwritten.\r\n"
                "DTSTART:20261103T170000Z\r\nDTEND:20261103T173000Z\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:own-2@google.com\r\nSUMMARY:Notes\r\nDESCRIPTION:I synced from work by calsync once.\r\n"
                "DTSTART:20261104T150000Z\r\nDTEND:20261104T160000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        tmin, tmax = "2026-11-01T00:00:00Z", "2026-11-30T00:00:00Z"
        by_title = {e["title"]: e for e in calsync.ics_events(feed, tmin, tmax)}
        self.assertIsNone(by_title["Coffee with Sam"]["copy_of"])
        self.assertTrue(by_title["Standup"]["copy_of"])
        self.assertIsNone(by_title["Notes"]["copy_of"])       # the note has to open the description, word for word

        class Feed:
            read_only = True
            def list(self, tmin, tmax):
                return calsync.ics_events(feed, tmin, tmax)
        a = FakeCal()
        calsync.sync([{"name": "personal"}, {"name": "a"}], {"personal": Feed(), "a": a}, now=NOW, log=lambda *_: None)
        self.assertEqual(sorted(e["title"] for e in a.copies()), ["Coffee with Sam", "Notes"])


SUN_11AM = dt.datetime(2026, 11, 1, 16, tzinfo=dt.timezone.utc)   # Sun Nov 1, 11:00 AM in New York


class ExportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = os.path.join(d.name, "export")
        self.a, self.b = FakeCal(), FakeCal()
        self.adapters = {"a": self.a, "b": self.b, "holding": FakeCal()}
        self.cals = [{"name": "a"}, {"name": "b", "label": "bee"}, {"name": "holding"}]
        self.cfg = {"folder": self.folder, "calendars": ["a", "b"]}

    def export(self):
        return calsync.export_calendars(self.cfg, self.cals, self.adapters, now=NOW)

    def read(self, label):
        with open(os.path.join(self.folder, label + ".ics"), encoding="utf-8", newline="") as f:
            return f.read()

    def parsed(self, label):
        return calsync.ics_events(self.read(label), "2026-01-01T00:00:00Z", "2028-01-01T00:00:00Z")

    def test_real_events_only(self):
        self.a.add("Real", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.a.add("A copy", "2026-11-02T17:00:00Z", "2026-11-02T18:00:00Z", copy_of="b|e1")
        self.a.add("Said no", "2026-11-02T19:00:00Z", "2026-11-02T20:00:00Z", declined=True)
        self.a.add("Canceled: Old", "2026-11-02T21:00:00Z", "2026-11-02T22:00:00Z")
        self.assertEqual(self.export(), {"a": 1, "bee": 0})
        self.assertEqual([e["title"] for e in self.parsed("a")], ["Real"])

    def test_copies_made_by_sync_do_not_appear(self):
        self.a.add("Real", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        run({"a": self.a, "b": self.b})
        self.assertEqual(self.export(), {"a": 1, "bee": 0})

    def test_meeting_on_two_accounts_lands_once_in_the_first_file(self):
        for cal in (self.b, self.a):
            cal.add("Shared", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", ical="same@x")
        self.assertEqual(self.export(), {"a": 1, "bee": 0})

    def test_each_repeat_of_an_event_stays_and_has_its_own_uid(self):
        self.a.add("Weekly", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", ical="w@x")
        self.a.add("Weekly", "2026-11-09T15:00:00Z", "2026-11-09T16:00:00Z", ical="w@x")
        self.assertEqual(self.export()["a"], 2)
        text = self.read("a")
        self.assertEqual(len({l for l in text.split("\r\n") if l.startswith("UID:")}), 2)
        self.assertNotIn("RRULE", text)

    def test_uid_is_stable_between_runs_and_when_an_event_moves(self):
        uid = self.a.add("Move", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.export()
        first = [l for l in self.read("a").split("\r\n") if l.startswith("UID:")]
        self.a.events[uid].update(start="2026-11-03T15:00:00Z", end="2026-11-03T16:00:00Z")
        self.export()
        self.assertEqual([l for l in self.read("a").split("\r\n") if l.startswith("UID:")], first)

    def test_format_round_trips_through_a_parser(self):
        title = "Café, sync; review\\plan\n" + "long " * 40
        self.a.add(title, "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", location="Room 4, HQ", busy=False)
        self.a.add("Trip", "2026-11-05", "2026-11-07")
        self.export()
        text = self.read("a")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertTrue(all(len(l.encode()) <= 75 for l in text.split("\r\n")))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        timed, day = self.parsed("a")
        self.assertEqual((timed["title"], timed["location"], timed["busy"]), (title, "Room 4, HQ", False))
        self.assertEqual((timed["start"], timed["end"], timed["all_day"]), ("2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", False))
        self.assertEqual((day["start"], day["end"], day["all_day"]), ("2026-11-05", "2026-11-07", True))

    def test_nothing_private_or_secret_is_written(self):
        self.a.add("Therapy", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z", private=True, location="Her office")
        self.a.add("Call", "2026-11-02T17:00:00Z", "2026-11-02T18:00:00Z", location="https://zoom.us/j/1?pwd=abc", attendees=["x@y.z"])
        self.export()
        text = self.read("a")
        for leaked in ("Therapy", "Her office", "zoom.us", "pwd", "x@y.z", "ATTENDEE"):
            self.assertNotIn(leaked, text)
        self.assertIn("SUMMARY:Busy", text)

    def test_window_is_30_days_back_and_a_year_ahead(self):
        self.a.add("Too old", "2026-09-01T15:00:00Z", "2026-09-01T16:00:00Z")
        self.a.add("Last month", "2026-10-05T15:00:00Z", "2026-10-05T16:00:00Z")
        self.a.add("Next year", "2027-10-30T15:00:00Z", "2027-10-30T16:00:00Z")
        self.a.add("Too far", "2027-11-05T15:00:00Z", "2027-11-05T16:00:00Z")
        self.export()
        self.assertEqual([e["title"] for e in self.parsed("a")], ["Last month", "Next year"])

    def test_a_failed_calendar_keeps_every_last_good_file(self):
        self.a.add("One", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.export()
        before = self.read("a"), self.read("bee")
        self.a.add("Two", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")
        self.b.fail = True
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertEqual((self.read("a"), self.read("bee")), before)

    def test_private_files_and_no_leftovers(self):
        self.export()
        self.assertEqual(sorted(os.listdir(self.folder)), ["a.ics", "bee.ics"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.folder, "a.ics")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)


class RunExportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        self.a, self.b = FakeCal(), FakeCal()
        self.a.add("Real", "2026-12-02T15:00:00Z", "2026-12-02T16:00:00Z")
        cfg = {"calendars": [{"name": "a"}, {"name": "b"}], "export": {"folder": os.path.join(d.name, "export"), "calendars": ["a", "b"]}}
        calsync.save_json(os.path.join(d.name, "config.json"), cfg)
        for name, value in (("HOME", d.name), ("CONFIG", os.path.join(d.name, "config.json")), ("LASTRUN", os.path.join(d.name, "last.json"))):
            p = mock.patch.object(calsync, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(calsync, "make_adapter", lambda c: {"a": self.a, "b": self.b}[c["name"]])
        p.start()
        self.addCleanup(p.stop)

    def run_it(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = calsync.main(["run", *args])
        return code, out.getvalue()

    def test_a_run_exports_and_records_the_counts(self):
        code, _ = self.run_it()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.home, "export/a.ics")))
        self.assertEqual(calsync.load_json(calsync.LASTRUN, {})["exported"], {"a": 1, "b": 0})

    def test_dry_run_exports_nothing(self):
        self.run_it("--dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.home, "export")))

    def test_a_failed_export_is_an_error_but_sync_still_ran(self):
        with mock.patch.object(calsync, "export_calendars", side_effect=RuntimeError("no")):
            code, out = self.run_it()
        self.assertEqual(code, 1)
        self.assertIn("! export: no", out)
        self.assertEqual(len(self.b.copies()), 1)


class NewCommandTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(calsync, "CONFIG", os.path.join(d.name, "config.json"))
        p.start()
        self.addCleanup(p.stop)
        calsync.save_json(calsync.CONFIG, {"calendars": [{"name": "main", "calendar_id": "main"}]})

    def check(self, text):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = calsync.main(["new", text, "--check"])
        return code, out.getvalue()

    def test_check_takes_odd_text_literally_and_writes_nothing(self):
        with mock.patch.object(calsync, "make_adapter", side_effect=AssertionError("no writes on --check")):
            for text, title in (("$(whoami) `id`; rm -rf x | y && z", "$(whoami) `id`; rm -rf x | y && z"),
                                ("--check the logs", "check the logs"),     # leading dashes are tidied, the rest stays
                                ("-x 'quoted' \"text\"", "x 'quoted' \"text\"")):
                code, out = self.check(text + " tomorrow 3pm")
                self.assertEqual(code, 0)
                self.assertTrue(out.startswith(title + ": "), out)

    def test_bad_text_exits_nonzero(self):
        for text in ("no day or time here", ""):
            with self.assertRaises(SystemExit) as e:
                self.check(text)
            self.assertTrue(str(e.exception.code).startswith("Not added"))


class QuickAddTests(unittest.TestCase):
    def when(self, text):
        ev, why = calsync.quick_parse(text, SUN_11AM)
        return f"{ev['title']} | {ev['when']}" if ev else "WARN " + why

    def test_phrases(self):
        for text, want in [
            ("Dentist tomorrow 3pm", "Dentist | Mon Nov 2, 3:00–4:00 PM"),
            ("Gym fri 6-7:30am", "Gym | Fri Nov 6, 6:00–7:30 AM"),
            ("Call Sam 11/3 at 2 for 30m", "Call Sam | Tue Nov 3, 2:00–2:30 PM"),
            ("Lunch with Sam on friday at noon for 1h30m", "Lunch with Sam | Fri Nov 6, 12:00–1:30 PM"),
            ("Meet at Starbucks fri 3pm", "Meet at Starbucks | Fri Nov 6, 3:00–4:00 PM"),
            ("Review 2026-11-05 15:00-16:30", "Review | Thu Nov 5, 3:00–4:30 PM"),
            ("Party sat 11-1pm", "Party | Sat Nov 7, 11:00 AM–1:00 PM"),
            ("Flight dec 3rd 6:15am 2h", "Flight | Thu Dec 3, 6:15–8:15 AM"),
            ("Write 2 hours tomorrow at 9", "Write | Mon Nov 2, 9:00–11:00 AM"),
            ("Trip nov 14 all day", "Trip | Sat Nov 14 (all day)"),
            ("Payday 11/15", "Payday | Sun Nov 15 (all day)"),
        ]:
            self.assertEqual(self.when(text), want, text)

    def test_times_already_passed_roll_forward(self):
        self.assertEqual(self.when("Standup 9:30am"), "Standup | Mon Nov 2, 9:30–10:30 AM")     # no day, 9:30 has passed
        self.assertEqual(self.when("Sync sun 9am"), "Sync | Sun Nov 8, 9:00–10:00 AM")          # today's 9am has passed
        self.assertEqual(self.when("Movie tonight 8pm"), "Movie | Sun Nov 1, 8:00–9:00 PM")
        self.assertEqual(self.when("Old 10/1"), "Old | Fri Oct 1 (all day)")                     # past date: next year
        self.assertEqual(calsync.quick_parse("Old 10/1", SUN_11AM)[0]["start"], "2027-10-01")

    def test_needs_a_day_or_time(self):
        self.assertTrue(self.when("Call mom").startswith("WARN add a day and time"))
        self.assertTrue(self.when("Read ch 3-4").startswith("WARN"))           # bare numbers are not times
        self.assertTrue(self.when("Haircut thursday 1.5h").startswith("WARN add a start time"))
        self.assertTrue(self.when("tomorrow 3pm").startswith("WARN add what it is"))

    def test_local_time_across_dst(self):
        ev, _ = calsync.quick_parse("Dentist tomorrow 3pm", SUN_11AM)
        self.assertEqual(ev["start"].isoformat(), "2026-11-02T15:00:00-05:00")   # EST after the Nov 1 change

    def test_note_lines_become_events_once(self):
        import os, tempfile

        class Main(FakeCal):
            def create_native(self, ev):
                return self.add(ev["title"], calsync.to_utc_z(ev["start"].isoformat()), calsync.to_utc_z(ev["end"].isoformat()))
        note = os.path.join(tempfile.mkdtemp(), "Add to calendar.md")
        with open(note, "w") as f:
            f.write("# Add to calendar\n\n- Dentist tomorrow 3pm\n- [ ] a task\n- Call mom\n    - indented\n"
                    "```\n- Example fri 3pm\n```\n- ✅ Done thing → Mon Nov 2\n")
        main = Main()
        self.assertEqual(calsync.quick_add_note(note, main, SUN_11AM, log=lambda *_: None), 1)
        lines = open(note).read().split("\n")
        self.assertIn("- ✅ Dentist tomorrow 3pm → Mon Nov 2, 3:00–4:00 PM", lines)
        self.assertIn('- Call mom ⚠️ add a day and time, e.g. "fri 3pm"', lines)
        self.assertIn("- [ ] a task", lines)              # checkboxes stay the task scheduler's
        self.assertIn("- Example fri 3pm", lines)         # code blocks are examples
        self.assertEqual([e["title"] for e in main.natives()], ["Dentist"])
        self.assertEqual(calsync.quick_add_note(note, main, SUN_11AM, log=lambda *_: None), 0)   # nothing twice
        with open(note, "w") as f:
            f.write("# Add to calendar\n\n- **Times:** 3pm, at 3\n\n## Events\n\n- Gym fri 6am\n")
        self.assertEqual(calsync.quick_add_note(note, main, SUN_11AM, log=lambda *_: None), 1)   # help bullets above stay text
        self.assertIn("- **Times:** 3pm, at 3", open(note).read())
        self.assertEqual(sorted(e["title"] for e in main.natives()), ["Dentist", "Gym"])
        del main.events[next(u for u, e in main.events.items() if e["title"] == "Gym")]
        b = FakeCal()
        run({"main": main, "b": b})
        self.assertEqual([(e["title"], e["start"]) for e in b.copies()], [("Dentist", "2026-11-02T20:00:00Z")])


class AgendaTests(unittest.TestCase):
    def setUp(self):
        import os, tempfile
        self.vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.vault, "Daily"))
        self.cal = FakeCal()
        self.cal.add("CSCI-C435", "2026-11-02T14:00:00Z", "2026-11-02T15:15:00Z")
        self.cal.add("Party", "2026-11-07T16:00:00Z", "2026-11-07T18:00:00Z")
        self.cal.add("Trip", "2026-11-03", "2026-11-05")
        self.cal.add("Nope", "2026-11-02T20:00:00Z", "2026-11-02T21:00:00Z", declined=True)
        self.cal.add("Canceled: Old sync", "2026-11-02T22:00:00Z", "2026-11-02T23:00:00Z")
        self.cal.add("Copy", "2026-11-02T16:00:00Z", "2026-11-02T17:00:00Z", copy_of="b|e1")
        self.cfg = {"vault": self.vault, "days": 14}

    def read(self, name):
        import os
        return open(os.path.join(self.vault, name)).read()

    def test_agenda_note(self):
        self.assertEqual(calsync.write_agenda(self.cfg, self.cal, SUN_11AM), 1)
        note = self.read("Calendar.md")
        self.assertIn("## Today · Sunday, November 1\n- Nothing scheduled", note)
        self.assertIn("## Tomorrow · Monday, November 2\n- 9:00–10:15 AM · CSCI-C435\n- 11:00 AM–12:00 PM · Copy\n", note)
        self.assertIn("## Tuesday, November 3\n- All day · Trip", note)
        self.assertIn("## Wednesday, November 4\n- All day · Trip", note)            # multi-day all-day event
        self.assertIn("## Saturday, November 7\n- 11:00 AM–1:00 PM · Party", note)
        self.assertNotIn("Thursday, November 5", note)                                # empty later days are left out
        self.assertNotIn("Nope", note)
        self.assertNotIn("Canceled", note)
        self.assertEqual(calsync.write_agenda(self.cfg, self.cal, SUN_11AM), 0)    # unchanged: nothing rewritten

    def test_daily_note_calendar_section(self):
        import os
        daily = "# Monday\n\n## 📅 Calendar\n- Calendar not connected yet\n\n## ✅ Tasks\n- [ ] mine\n"
        with open(os.path.join(self.vault, "Daily", "2026-11-02.md"), "w") as f:
            f.write(daily)
        calsync.write_agenda(self.cfg, self.cal, SUN_11AM)
        self.assertEqual(self.read("Daily/2026-11-02.md"),
                         "# Monday\n\n## 📅 Calendar\n- 9:00–10:15 AM · CSCI-C435\n- 11:00 AM–12:00 PM · Copy\n\n## ✅ Tasks\n- [ ] mine\n")
        self.cal.add("Dentist", "2026-11-02T21:00:00Z", "2026-11-02T22:00:00Z")
        calsync.write_agenda(self.cfg, self.cal, SUN_11AM)
        self.assertIn("- 4:00–5:00 PM · Dentist\n\n## ✅ Tasks\n- [ ] mine", self.read("Daily/2026-11-02.md"))
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Daily", "2026-11-03.md")))   # never creates daily notes


class HostileCalendarTests(unittest.TestCase):
    """Anyone can put an event on a calendar by sending an invite, and a feed can be anyone's."""
    TMIN, TMAX = "2030-01-01T00:00:00Z", "2030-03-01T00:00:00Z"
    GOOD = "BEGIN:VEVENT\r\nUID:good\r\nDTSTART:20300110T150000Z\r\nDTEND:20300110T160000Z\r\nSUMMARY:Good\r\nEND:VEVENT\r\n"

    def titles(self, *events, **kw):
        text = "BEGIN:VCALENDAR\r\n" + "".join(events) + "END:VCALENDAR\r\n"
        with contextlib.redirect_stderr(io.StringIO()):
            return [e["title"] for e in calsync.ics_events(text, self.TMIN, self.TMAX, **kw)]

    def bad(self, *lines):
        return "BEGIN:VEVENT\r\nUID:bad\r\nSUMMARY:Bad\r\n" + "".join(l + "\r\n" for l in lines) + "END:VEVENT\r\n"

    def test_one_unreadable_event_does_not_stop_the_calendar(self):
        for lines in (["DTSTART:notadate"], ["DTSTART:20300110T150000Z", "RRULE:FREQ=NOPE"],
                      ["DTSTART:20300110T150000Z", "DURATION:P99999999999W"], ["DTSTART:20300110T150000Z", "RDATE:notadate"],
                      ["DTSTART:20300110T150000Z", "RECURRENCE-ID:notadate"]):
            self.assertEqual(self.titles(self.GOOD, self.bad(*lines)), ["Good"], lines)

    def test_a_rule_repeating_every_minute_or_second_is_left_out_quickly(self):
        started = time.monotonic()
        for rule in ("FREQ=SECONDLY", "FREQ=MINUTELY", "freq=minutely;interval=1", "INTERVAL=1;FREQ=SECONDLY"):
            self.assertEqual(self.titles(self.GOOD, self.bad("DTSTART:20000101T000000Z", "RRULE:" + rule)), ["Good"])
        self.assertLess(time.monotonic() - started, 5)

    def test_hourly_repeats_still_work(self):
        self.assertEqual(len(self.titles(self.bad("DTSTART:20300110T150000Z", "RRULE:FREQ=HOURLY;COUNT=3"))), 3)

    def test_a_calendar_past_its_time_limit_is_reported_not_half_read(self):
        with self.assertRaisesRegex(RuntimeError, "too many repeating"):
            self.titles(self.GOOD, deadline=time.monotonic() - 1)

    def test_a_line_folded_millions_of_times_is_read_in_a_moment(self):
        started = time.monotonic()
        text = "BEGIN:VEVENT\r\nUID:u\r\nSUMMARY:x" + "\r\n y" * 2_000_000 + "\r\nDTSTART:20300110T150000Z\r\nEND:VEVENT\r\n"
        self.assertEqual(len(calsync.parse_ics(text)[0]["SUMMARY"][0][1]), 2_000_001)
        self.assertLess(time.monotonic() - started, 5)

    def test_folding_rules_are_unchanged(self):
        ev = calsync.parse_ics("BEGIN:VEVENT\nSUMMARY:a\n b\n\tc\n  d\r\n e\rUID:u\nEND:VEVENT\n")[0]
        self.assertEqual(ev["SUMMARY"][0][1], "abc de")
        self.assertEqual(ev["UID"][0][1], "u")

    def test_a_feed_too_big_to_read_whole_is_an_error_not_a_shorter_calendar(self):
        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(calsync.IcsCalendar, "MAX_BYTES", 40), mock.patch("urllib.request.urlopen", lambda *a, **k: Reply(b"BEGIN:VCALENDAR" + b"x" * 40)):
            with self.assertRaisesRegex(RuntimeError, "over 50 MB"):
                calsync.IcsCalendar("https://example.com/a.ics").list(self.TMIN, self.TMAX)

    def test_a_link_with_a_space_never_lands_in_an_error_with_its_secret(self):
        with self.assertRaises(RuntimeError) as seen:
            calsync.IcsCalendar("https://example.com/feed token=SECRETPART")
        self.assertNotIn("SECRETPART", str(seen.exception))

    def test_a_busy_or_private_copy_does_not_name_its_source(self):
        class Dst:
            read_only = False

            def __init__(self):
                self.labels = {}

            def list(self, a, b):
                return []

            def create(self, fields, key, label):
                self.labels[fields["title"]] = label
        ev = dict(uid="1", ical="1", title="Open", start="2030-01-10T15:00:00Z", end="2030-01-10T16:00:00Z", all_day=False,
                  busy=True, location="", private=False, declined=False, copy_of=None)
        class Src(Dst):
            def list(self, a, b):
                return [ev, dict(ev, uid="2", ical="2", title="Secret", private=True)]
        for busy_only, want in ((False, {"Open": "Therapy", "Busy": "another calendar"}), (True, {"Busy": "another calendar"})):
            dst = Dst()
            calsync.sync([{"name": "a", "label": "Therapy", "read_only": True}, {"name": "b", "busy_only": busy_only}], {"a": Src(), "b": dst}, log=lambda *_: None)
            self.assertEqual(dst.labels, want)


class QuickPlanTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 26, 17, tzinfo=dt.timezone.utc)

    def plan(self, text):
        return calsync.quick_plan(text, self.NOW)

    def test_plain_words_with_a_length(self):
        ev, _ = self.plan("breakfast with friends tomorrow 9am for 2 hours")
        self.assertEqual((ev["title"], ev["guests"], ev["remind_minutes"]), ("breakfast with friends", [], None))
        self.assertEqual(ev["end"] - ev["start"], dt.timedelta(hours=2))

    def test_addresses_become_guests_and_names_stay_in_the_title(self):
        ev, _ = self.plan("Breakfast with Sam and SAM@x.com sat 9am 2h remind me 1h before")
        self.assertEqual((ev["title"], ev["guests"], ev["remind_minutes"]), ("Breakfast with Sam", ["sam@x.com"], 60))
        ev, _ = self.plan("lunch with a@b.co, c@d.org, a@b.co fri noon, reminder 30 min")
        self.assertEqual((ev["title"], ev["guests"], ev["remind_minutes"]), ("lunch", ["a@b.co", "c@d.org"], 30))

    def test_limits(self):
        self.assertIsNone(self.plan("party " + " ".join(f"g{i}@x.com" for i in range(25)) + " fri 8pm")[0])
        self.assertIsNone(self.plan("trip fri 8pm remind me 30 days before")[0])
        self.assertIsNone(self.plan("coffee with sam@x.com")[0])          # no day or time: nothing to add


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeCal(), FakeCal()
        self.cals = {"a": self.a, "b": self.b}
        self.uid = self.a.add("Class", "2026-11-02T15:00:00Z", "2026-11-02T16:00:00Z")
        self.a.add("Lab", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")

    def test_a_skipped_occurrence_loses_its_copies_and_its_source_stays(self):
        run(self.cals)
        self.assertEqual(len(self.b.copies()), 2)
        with mock.patch.object(calsync, "skipped", return_value={f"a|{self.uid}"}):
            s = run(self.cals)
            self.assertEqual(s["deleted"], 1)
            self.assertEqual([e["title"] for e in self.b.copies()], ["Lab"])
            self.assertEqual(run(self.cals)["created"], 0)          # stays skipped though the source still lists it
        self.assertEqual(len(self.a.natives()), 2)
        self.assertEqual(run(self.cals)["created"], 1)              # undo: copied again

    def test_skips_are_kept_in_the_config_and_drop_off_after_their_day(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with mock.patch.object(calsync, "CONFIG", os.path.join(d.name, "config.json")):
            calsync.save_json(calsync.CONFIG, {"calendars": []})
            soon = (calsync.now_utc() + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            calsync.skip_occurrence("school|x|1", "Class", soon)
            calsync.skip_occurrence("school|old|1", "Old", "2020-01-01T10:00:00Z")
            self.assertEqual(calsync.skipped(), {"school|x|1"})
            calsync.unskip_occurrence("school|x|1")
            self.assertEqual(calsync.skipped(), set())
            with self.assertRaises(ValueError):
                calsync.skip_occurrence("not a key", "x", soon)


class GoogleInviteTests(unittest.TestCase):
    def test_guests_get_an_invite_and_a_reminder_is_set(self):
        ev, _ = calsync.quick_plan("Breakfast with sam@x.com sat 9am 2h remind me 1h before", dt.datetime(2026, 11, 26, 17, tzinfo=dt.timezone.utc))
        g = calsync.GoogleCalendar("acct", "me@example.com")
        with mock.patch.object(calsync, "google_access_token", return_value="t"), \
             mock.patch.object(calsync, "http", return_value={"id": "new"}) as http:
            g.create_native(ev)
        method, url, _, body = http.call_args[0]
        self.assertTrue(url.endswith("/events?sendUpdates=all"))
        self.assertEqual(body["attendees"], [{"email": "sam@x.com"}])
        self.assertEqual(body["reminders"]["overrides"], [{"method": "popup", "minutes": 60}])

    def test_no_guests_no_emails(self):
        ev, _ = calsync.quick_plan("Dentist sat 9am", dt.datetime(2026, 11, 26, 17, tzinfo=dt.timezone.utc))
        g = calsync.GoogleCalendar("acct", "me@example.com")
        with mock.patch.object(calsync, "google_access_token", return_value="t"), \
             mock.patch.object(calsync, "http", return_value={"id": "new"}) as http:
            g.create_native(ev)
        self.assertTrue(http.call_args[0][1].endswith("/events"))
        self.assertNotIn("attendees", http.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
