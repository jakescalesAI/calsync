"""Windows has no fcntl, so these load the engine as Windows would and stand in for msvcrt and winreg."""
import importlib.util, os, sys, tempfile, types, unittest
from unittest import mock


def load_as_windows(msvcrt):
    spec = importlib.util.spec_from_file_location("calsync_win", os.path.join(os.path.dirname(__file__) or ".", "calsync.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": msvcrt}):
        spec.loader.exec_module(module)
    return module


class FakeMsvcrt(types.ModuleType):
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        super().__init__("msvcrt")
        self.held = set()

    def locking(self, fd, mode, size):
        name = os.path.realpath(f"/proc/self/fd/{fd}")
        if mode == self.LK_UNLCK:
            self.held.discard(name)
        elif name in self.held:
            raise OSError(13, "locked")
        else:
            self.held.add(name)


class WindowsTest(unittest.TestCase):
    def setUp(self):
        self.msvcrt = FakeMsvcrt()
        self.engine = load_as_windows(self.msvcrt)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine.HOME = self.tmp.name
        self.engine.SECRETS = os.path.join(self.tmp.name, "secrets")

    def test_engine_loads_without_fcntl(self):
        self.assertIsNone(self.engine.fcntl)

    def test_a_held_lock_refuses_a_second_run_then_frees(self):
        with self.engine.file_lock("run.lock", blocking=False) as first:
            self.assertTrue(first)
            with self.engine.file_lock("run.lock", blocking=False) as second:
                self.assertFalse(second)
        with self.engine.file_lock("run.lock", blocking=False) as again:
            self.assertTrue(again)

    def test_zone_comes_from_the_registry(self):
        key = mock.MagicMock()
        key.__enter__.return_value = key
        reg = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=lambda *a: key,
                                    QueryValueEx=lambda k, n: ("Eastern Standard Time", 1))
        with mock.patch.dict(sys.modules, {"winreg": reg}):
            self.assertEqual(self.engine.windows_zone(), "America/New_York")
        reg.QueryValueEx = lambda k, n: ("Somewhere Else Time", 1)
        with mock.patch.dict(sys.modules, {"winreg": reg}):
            self.assertEqual(self.engine.windows_zone(), "")

    def test_no_registry_means_no_zone(self):
        with mock.patch.dict(sys.modules, {"winreg": None}):
            self.assertEqual(self.engine.windows_zone(), "")

    def registry(self, name):
        key = mock.MagicMock()
        key.__enter__.return_value = key
        return types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=lambda *a: key, QueryValueEx=lambda k, n: (name, 1))

    def test_zones_the_table_doesnt_know_come_from_windows_icu(self):
        asked = []
        with mock.patch.dict(sys.modules, {"winreg": self.registry("US Eastern Standard Time")}),                 mock.patch.object(self.engine, "icu_zone", side_effect=lambda n: asked.append(n) or "America/Indianapolis"):
            self.assertEqual(self.engine.windows_zone(), "America/Indianapolis")
        self.assertEqual(asked, ["US Eastern Standard Time"])
        with mock.patch.dict(sys.modules, {"winreg": self.registry("Eastern Standard Time")}),                 mock.patch.object(self.engine, "icu_zone", side_effect=AssertionError("the table knows this one")):
            self.assertEqual(self.engine.windows_zone(), "America/New_York")

    def test_icu_zone_asks_icu_dll(self):
        import ctypes

        def lookup(name, length, default, out, size, status):
            if name != "US Eastern Standard Time":
                status._obj.value = 1            # U_ILLEGAL_ARGUMENT_WARNING-ish: not found
                return 0
            out.value = "America/Indianapolis"
            return len(out.value)
        dll = types.SimpleNamespace(ucal_getTimeZoneIDForWindowsID=mock.MagicMock(side_effect=lookup))
        self.engine._icu.clear()
        with mock.patch.object(ctypes, "WinDLL", return_value=dll, create=True) as loaded:
            self.assertEqual(self.engine.icu_zone("US Eastern Standard Time"), "America/Indianapolis")
            self.assertEqual(self.engine.icu_zone("Nowhere Standard Time"), "")
            self.assertEqual(self.engine.icu_zone("US Eastern Standard Time"), "America/Indianapolis")   # asked once
        loaded.assert_called_with("icu.dll")
        self.assertEqual(dll.ucal_getTimeZoneIDForWindowsID.call_count, 2)
        self.engine._icu.clear()
        with mock.patch.object(ctypes, "WinDLL", side_effect=OSError("no icu.dll"), create=True):  # before Windows 10 1703
            self.assertEqual(self.engine.icu_zone("US Eastern Standard Time"), "")
        self.engine._icu.clear()

    def test_no_zone_found_is_said_not_silently_utc(self):
        self.engine.set_home(self.tmp.name)
        with mock.patch.dict(os.environ, {"CALSYNC_TZ": ""}), mock.patch.object(self.engine, "system_zone", return_value=""):
            self.assertEqual(self.engine.zone_problem(), "unknown")
            self.assertEqual(self.engine.tz_name(), "UTC")
            self.assertEqual(self.engine.local_tz().utcoffset(self.engine.dt.datetime(2026, 7, 1)), self.engine.dt.timedelta(0))
            self.engine.save_json(self.engine.CONFIG, {"calendars": [], "timezone": "America/Chicago"})   # chosen in Settings
            self.assertIsNone(self.engine.zone_problem())
            self.assertEqual(str(self.engine.local_tz()), "America/Chicago")
            self.engine.save_json(self.engine.CONFIG, {"calendars": [], "timezone": "Mars/Olympus_Mons"})
            self.assertEqual(self.engine.zone_problem(), "bad-name")
        with mock.patch.dict(os.environ, {"CALSYNC_TZ": "America/New_York"}):
            self.assertIsNone(self.engine.zone_problem())
            import zoneinfo                            # Windows Python without the tzdata package
            with mock.patch.object(zoneinfo, "ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("no tzdata")):
                self.assertEqual(self.engine.zone_problem(), "no-data")
                self.assertIs(self.engine.local_tz(), self.engine.dt.timezone.utc)
        for problem in ("unknown", "no-data", "bad-name"):
            self.assertIn("UTC", self.engine.ZONE_WORDS[problem])
        self.assertIn("pip install tzdata", self.engine.ZONE_WORDS["no-data"])


if __name__ == "__main__":
    unittest.main()
